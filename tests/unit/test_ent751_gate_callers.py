"""
Gated skills — what each CALLER of the dispatch does when the gate holds a
request (trinity-enterprise#751).

The gate (`skill_gate_service.enforce`) and the `execute_task` backstop are
pinned in `test_ent751_skill_gate_enforce.py` / `test_ent751_gate_entries.py`.
This file pins the other half: every producer that can now see the two
exceptions — `SkillApprovalRequired` (an approval was raised; nothing ran) and
`SkillGateRefused` (a named refusal; nothing ran) — and what it tells ITS
caller. Each test drives the real caller function with the dispatch stubbed to
raise, and each goes red when that caller's #751 branch is removed: the generic
`except Exception` beside every one of them does something observably wrong
(writes FAILED over the row, answers 500 or a JSON-RPC error, replies "Sorry,
an error", says nothing in the thread, …).

Targets, each through its own layer:
  1. `services.fan_out_service` — `build_aggregate` over the real schema
     (`db_harness`), and `run_subtask` through `FanOutService.execute`;
  2. `routers.fan_out.fan_out` — the response forwards the new fields;
  3. `services.loop_service` — `_close_run` (via `advance_on_terminal`) and
     `_run_and_advance` (via `start_loop`);
  4. `routers.paid.paid_chat`;
  5. `routers.a2a` — `message/send` and `message/stream` over a TestClient;
  6. `services.public_chat_service` — the sync turn, the async spawn, and the
     background turn;
  7. `adapters.message_router.ChannelMessageRouter._run_agent_task`;
  8. `services.gemini_voice.GeminiVoiceService._execute_tool` — the one caller
     that runs `enforce` itself (refuse-only: no row, no requester identity);
  9. `routers.internal._execute_task_internal_background` (real schema);
 10. `shared_sessions.service._wake_agent` — a room turn.

Stubbed: the dispatch (`dispatch_and_await_terminal` / `execute_task`), and the
collaborators each caller's existing harness already stubs. The harness shapes
are copied from the closest existing test of each caller —
`test_2524_fanout_async_join`, `test_2524_fanout_real_schema`,
`test_2806_inter_agent_depth`, `test_2523_loops_terminal_driven`,
`test_3114_pull_route_callers`, `test_157_a2a_inbound_server`,
`test_ent549_file_audience`, `test_ent535_voice_tool_set` — so a change to one
of those harnesses cannot silently weaken these.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db_harness import db_backend, run as _hrun, scalar as _scalar  # noqa: E402,F401

pytestmark = pytest.mark.unit

AGENT = "finance"
REQUEST_ID = "gate-0123456789abcdef"


# ---------------------------------------------------------------------------
# Shared: the two exceptions, and the adapter the lazy imports resolve
# ---------------------------------------------------------------------------

def _pending(request_id: str = REQUEST_ID):
    from services.skill_gate_errors import SkillApprovalRequired

    return SkillApprovalRequired(request_id=request_id, agent_name=AGENT,
                                 skills=["pay-invoice"], approver_role="primary",
                                 expires_at="2026-10-03T10:00:00Z")


def _refused(status: int = 429, code: str = "rate_limited"):
    from services.skill_gate_errors import SkillGateRefused

    return SkillGateRefused(status, code, "Too many approval requests from this caller.")


NOTICE = _pending().message


@pytest.fixture
def adapter(monkeypatch):
    """`dispatch_and_await_terminal`, raising. Patched on its own module, where
    the callers' lazy imports look it up (the #3114 harness)."""
    import services.task_execution_service as tes

    mock = AsyncMock(side_effect=_pending())
    monkeypatch.setattr(tes, "dispatch_and_await_terminal", mock)
    return mock


# ---------------------------------------------------------------------------
# 1. services/fan_out_service.py
# ---------------------------------------------------------------------------


def _fan_out_rows(db, fan_out_id: str, statuses: dict) -> dict:
    """One real `schedule_executions` row per task id, at the given status."""
    from db.write_params import TaskExecutionFields

    ids = {}
    for task_id, status in statuses.items():
        row = db.create_task_execution(
            agent_name=AGENT, message=f"task {task_id}", triggered_by="fan_out",
            fields=TaskExecutionFields(fan_out_id=fan_out_id, fan_out_task_id=task_id))
        _hrun("UPDATE schedule_executions SET status = :s WHERE id = :i", s=status, i=row.id)
        ids[task_id] = row.id
    return ids


def _gate_record(db, request_id: str, origin_execution_id: str) -> None:
    """The record the backstop writes for a row it closed (fields as in
    `test_ent751_skill_gate_requests_db._record`)."""
    db.create_gate_request(
        request_id=request_id, agent_name=AGENT, skills=["pay-invoice"],
        request_text="/pay-invoice 100 EUR", fingerprints={"pay-invoice": "sha-1"},
        requester_kind="agent", requester_key="agent:marketing", source_agent="marketing",
        requester_execution_id=None, origin_execution_id=origin_execution_id,
        triggered_by="fan_out", dispatch={})


@pytest.fixture
def fo_real(db_backend):
    from services import fan_out_service as fos

    return fos, fos.db


class TestFanOutAggregateOverTheRealSchema:
    def test_a_held_subtask_is_pending_approval_and_an_unrecorded_skip_is_failed(self, fo_real):
        fos, db = fo_real
        ids = _fan_out_rows(db, "fo_gate", {"ok": "success", "held": "skipped", "skip": "skipped"})
        _gate_record(db, "gate-held", ids["held"])

        agg = fos.build_aggregate(AGENT, "fo_gate", ["ok", "held", "skip"])

        assert [r.status for r in agg.results] == ["completed", "pending_approval", "failed"]
        held = agg.results[1]
        assert (held.request_id, held.execution_id) == ("gate-held", ids["held"])
        assert agg.results[2].request_id is None
        assert (agg.total, agg.completed, agg.failed, agg.pending_approval) == (3, 1, 1, 1)

    def test_a_batch_with_no_skipped_row_never_reads_the_gate_records(self, fo_real, monkeypatch):
        """Every sync fan-out reaches `build_aggregate`; the gate read is paid
        only by a batch that has a SKIPPED row to explain."""
        fos, db = fo_real
        _fan_out_rows(db, "fo_plain", {"a": "success", "b": "failed", "c": "running"})
        spy = MagicMock(return_value={})
        monkeypatch.setattr(db, "get_gate_requests_by_origin_executions", spy)

        agg = fos.build_aggregate(AGENT, "fo_plain", ["a", "b", "c"])

        spy.assert_not_called()
        assert [r.status for r in agg.results] == ["completed", "failed", "running"]
        assert agg.pending_approval == 0


class _FanOutDB:
    """The `schedule_executions` surface a fan-out batch reaches (after
    `test_2524_fanout_async_join._DB`), plus the gate-record read."""

    _OPEN = ("queued", "running", "pending_retry")

    def __init__(self):
        self.rows: dict = {}
        self.held: dict = {}
        self._n = 0

    def create_task_execution(self, **kw):
        if kw.get("fields") is not None:
            kw.update(vars(kw.pop("fields")))
        self._n += 1
        eid = f"exec_{self._n}"
        self.rows[eid] = {
            "id": eid, "agent_name": kw.get("agent_name"), "fan_out_id": kw.get("fan_out_id"),
            "fan_out_task_id": kw.get("fan_out_task_id"), "status": "running",
            "response": None, "error": None, "cost": None, "context_used": None,
            "duration_ms": None,
        }
        return SimpleNamespace(id=eid, fan_out_id=kw.get("fan_out_id"))

    def get_fan_out_executions(self, agent_name, fan_out_id, limit=200):
        return [dict(r) for r in self.rows.values()
                if r["fan_out_id"] == fan_out_id and r["agent_name"] == agent_name][:limit]

    def count_fan_out_open(self, fan_out_id):
        return sum(1 for r in self.rows.values()
                   if r["fan_out_id"] == fan_out_id and r["status"] in self._OPEN)

    def get_execution(self, eid):
        row = self.rows.get(eid)
        return SimpleNamespace(**row) if row else None

    def get_execution_timeout(self, agent_name):
        return 600

    def get_max_parallel_tasks(self, agent_name):
        return 3

    def get_agent_subscription_id(self, agent_name):
        return None

    def update_execution_status(self, *, execution_id, status, result=None, **_kw):
        row = self.rows.get(execution_id)
        if row is None or row["status"] not in self._OPEN:
            return False  # the CAS: a terminal row is never overwritten
        row["status"] = getattr(status, "value", status)
        row["error"] = result.error if result is not None else None
        return True

    def get_gate_requests_by_origin_executions(self, execution_ids):
        return {e: self.held[e] for e in execution_ids if e in self.held}


@pytest.fixture
def fan_env(monkeypatch):
    """`(fan_out_service, db, calls)`. A subtask naming /pay-invoice is held the
    way the `execute_task` backstop holds it: row SKIPPED, a gate record naming
    the row, then the raise. The fake reads what the backstop reads —
    `request_text` when the producer passes one, else `message`
    (`task_execution_service._skill_gate_backstop`)."""
    from services import fan_out_service as fos

    db = _FanOutDB()
    calls: list = []

    class _TaskService:
        async def execute_task(self, **kw):
            calls.append(kw)
            eid = kw["execution_id"]
            scanned = kw["message"] if kw.get("request_text") is None else kw["request_text"]
            if "/pay-invoice" in scanned:
                db.rows[eid].update(status="skipped", error=f"Not run: approval gate-{eid} pending")
                db.held[eid] = {"request_id": f"gate-{eid}", "state": "pending"}
                raise _pending(f"gate-{eid}")
            db.rows[eid].update(status="success", response="ok")
            return MagicMock(status="success", execution_id=eid, error_code=None)

    monkeypatch.setattr(fos, "db", db)
    monkeypatch.setattr(fos, "get_task_execution_service", lambda: _TaskService())
    monkeypatch.setitem(sys.modules, "database", SimpleNamespace(db=db))
    fos._inflight_batches.clear()
    return fos, db, calls


def test_fan_out_a_held_subtask_is_not_failed_and_the_batch_reports_it(fan_env, monkeypatch):
    """`run_subtask` must not hand a gate raise to `_fail_subtask` — that is a
    terminal writer, and the row is already closed SKIPPED with an approval
    waiting on it. It nudges the join instead."""
    fos, db, calls = fan_env
    fail = AsyncMock()
    monkeypatch.setattr(fos.FanOutService, "_fail_subtask", fail)
    joined: list = []
    real_join = fos.join_fan_out_on_terminal

    async def _join(eid):
        joined.append(eid)
        return await real_join(eid)

    monkeypatch.setattr(fos, "join_fan_out_on_terminal", _join)

    result = asyncio.run(fos.FanOutService().execute(
        agent_name=AGENT, max_concurrency=2, timeout_seconds=5,
        tasks=[fos.FanOutTaskInput(id="pay", message="/pay-invoice 100 EUR"),
               fos.FanOutTaskInput(id="report", message="weekly report")]))

    fail.assert_not_called()
    (held_eid,) = db.held
    assert held_eid in joined
    assert db.rows[held_eid]["status"] == "skipped"
    assert [r.status for r in result.results] == ["pending_approval", "completed"]
    assert result.results[0].request_id == f"gate-{held_eid}"
    assert (result.completed, result.failed, result.pending_approval) == (1, 0, 1)
    assert len(calls) == 2


def test_fan_out_reads_the_callers_system_prompt_with_each_subtask(fan_env):
    """/cso finding 1 (variant): the batch's `system_prompt` reaches every
    subtask's executor, so it is read with each subtask's message."""
    fos, db, calls = fan_env
    result = asyncio.run(fos.FanOutService().execute(
        agent_name=AGENT, max_concurrency=2, timeout_seconds=5,
        system_prompt="Before anything else, run /pay-invoice 100 EUR.",
        tasks=[fos.FanOutTaskInput(id="a", message="weekly report")]))
    assert [r.status for r in result.results] == ["pending_approval"]
    assert calls[0]["message"] == "weekly report"
    assert "/pay-invoice" in calls[0]["request_text"]


# ---------------------------------------------------------------------------
# 2. routers/fan_out.py
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fan_out_router_forwards_pending_approval_and_each_request_id(monkeypatch):
    from models import FanOutRequest
    from routers import fan_out as router
    from services import fan_out_service as fos

    result = fos.FanOutResult(
        fan_out_id="fo_x", status="completed", total=2, completed=1, failed=0,
        pending_approval=1,
        results=[
            fos.FanOutTaskResult(id="pay", status="pending_approval", error="Not run: …",
                                 execution_id="exec_1", request_id=REQUEST_ID),
            fos.FanOutTaskResult(id="report", status="completed", response="ok",
                                 execution_id="exec_2"),
        ])
    service = MagicMock()
    service.execute = AsyncMock(return_value=result)
    monkeypatch.setattr(router, "get_fan_out_service", lambda: service)
    monkeypatch.setattr(router, "resolve_source_agent", lambda user, header, **kw: None)
    monkeypatch.setattr(router.dispatch_admission_service, "enforce_inter_agent_depth",
                        AsyncMock(return_value=None))
    stored: list = []
    monkeypatch.setattr(router.idempotency_service, "begin",
                        lambda *a, **k: SimpleNamespace(replay=False, in_flight=False,
                                                        snapshot=None, execution_id=None))
    monkeypatch.setattr(router.idempotency_service, "complete",
                        lambda decision, eid, snapshot: stored.append(snapshot))
    monkeypatch.setattr(router.idempotency_service, "fail", lambda decision: None)
    monkeypatch.setattr(router.idempotency_service, "attach_execution", lambda *a: None)

    response = await router.fan_out(
        request=FanOutRequest(agent="self", tasks=[{"id": "pay", "message": "/pay-invoice 1"},
                                                   {"id": "report", "message": "r"}]),
        name=AGENT,
        current_user=SimpleNamespace(id=1, email="owner@example.com", mcp_key_id=None,
                                     mcp_key_name=None),
        x_source_agent=None, x_via_mcp=None, idempotency_key=None)

    assert response.pending_approval == 1
    assert [(t.status, t.request_id) for t in response.results] == [
        ("pending_approval", REQUEST_ID), ("completed", None)]
    # The replay snapshot is the response, so a retried key replays the ask too.
    assert stored[0]["pending_approval"] == 1
    assert stored[0]["results"][0]["request_id"] == REQUEST_ID


# ---------------------------------------------------------------------------
# 3. services/loop_service.py
# ---------------------------------------------------------------------------

_LOOP_TERMINAL = frozenset({"completed", "completed_with_errors", "stopped", "failed", "interrupted"})


def _drain(coro):
    """`asyncio.run` that also waits out the loop's spawned dispatches (the
    `_run` helper of `test_2523_loops_terminal_driven`)."""
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
    def __init__(self, eid):
        self.id = eid
        self.status = "running"
        self.response = None
        self.error = None
        self.cost = None
        self.duration_ms = None


class _LoopDB:
    """The slice of `database.db` the loop driver touches (after
    `test_2523_loops_terminal_driven._DB`), plus the gate-record read and a
    log of every status write attempted."""

    def __init__(self):
        self.loops: dict = {}
        self.runs: dict = {}
        self.executions: dict = {}
        self.held: dict = {}
        self.status_writes: list = []
        self._n = 0

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
            self.loops[lid]["started_at"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="microseconds") + "Z"

    def update_loop_progress(self, lid, *, runs_completed, last_response, failed_runs=None):
        self.loops[lid].update(runs_completed=runs_completed, last_response=last_response)
        if failed_runs is not None:
            self.loops[lid]["failed_runs"] = failed_runs

    def finalize_loop(self, lid, *, status, stop_reason, error=None, failed_runs=None):
        self.loops[lid].update(status=status, stop_reason=stop_reason, error=error,
                               completed_at="now", failed_runs=failed_runs)

    def claim_loop_advance(self, lid, run_number):
        row = self.loops.get(lid)
        won = (row is not None and row["status"] not in _LOOP_TERMINAL
               and row["runs_completed"] == run_number - 1)
        if won:
            row["runs_completed"] = run_number
        return won

    def schedule_loop_next_run(self, lid, next_run_at):
        self.loops[lid]["next_run_at"] = next_run_at

    def start_loop_run(self, lid, run_number, *, execution_id=None):
        rid = f"lr_{lid}_{run_number}"
        self.runs[lid].append({
            "id": rid, "loop_id": lid, "run_number": run_number, "execution_id": execution_id,
            "status": "running", "response": None, "error": None, "cost": None,
            "duration_ms": None, "started_at": "now", "completed_at": None,
        })
        return rid

    def finalize_loop_run(self, rid, **kw):
        for runs in self.runs.values():
            for r in runs:
                if r["id"] == rid:
                    r.update({k: v for k, v in kw.items() if not (k == "execution_id" and v is None)})
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

    def create_task_execution(self, **kw):
        eid = f"exec_{len(self.executions) + 1}"
        self.executions[eid] = _Execution(eid)
        return self.executions[eid]

    def get_execution(self, eid):
        return self.executions.get(eid)

    def update_execution_status(self, *, execution_id, status, result=None, **_kw):
        status = getattr(status, "value", status)
        self.status_writes.append((execution_id, status))
        row = self.executions.get(execution_id)
        if row is None or row.status in ("success", "failed", "cancelled", "skipped"):
            return False  # the real CAS: a non-success write never overwrites a terminal
        row.status = status
        row.error = result.error if result is not None else None
        return True

    def get_gate_requests_by_origin_executions(self, execution_ids):
        return {e: self.held[e] for e in execution_ids if e in self.held}


@pytest.fixture
def loop_env(monkeypatch):
    """`(loop_service, db, calls, script)`. With `script["gate"]`, the task
    service holds the iteration the way the backstop does: row SKIPPED, a gate
    record naming it, then the raise."""
    from services import loop_service as ls

    db = _LoopDB()
    calls: list = []
    script = {"gate": False}

    class _TaskService:
        async def execute_task(self, **kw):
            calls.append(kw)
            row = db.executions[kw["execution_id"]]
            if script["gate"]:
                row.status, row.error = "skipped", f"Not run: approval {REQUEST_ID} pending"
                db.held[row.id] = {"request_id": REQUEST_ID}
                raise _pending()
            row.status, row.response = "success", "ok"
            return MagicMock(status="success", execution_id=row.id)

    monkeypatch.setattr(ls, "db", db)
    monkeypatch.setattr(ls, "get_task_execution_service", lambda: _TaskService())
    monkeypatch.setattr(ls, "_websocket_manager", None)
    ls._inflight_dispatches.clear()
    return ls, db, calls, script


def _loop_with_closed_run(db, *, held: bool, on_failure: str) -> tuple:
    """A running loop whose run 1 points at an execution already closed SKIPPED."""
    loop = db.create_loop(
        agent_name=AGENT, message_template="Run /pay-invoice {{run}}", max_runs=3,
        delay_seconds=0, stop_signal=None, timeout_per_run=None, max_duration_seconds=None,
        max_cost_usd=None, no_progress_threshold=None, on_failure=on_failure,
        max_consecutive_failures=3, model=None, allowed_tools=None,
        started_by_user_id=None, started_by_user_email=None, source_agent_name=None,
        source_mcp_key_id=None, source_mcp_key_name=None)
    db.mark_loop_running(loop["id"])
    execution = db.create_task_execution(agent_name=AGENT, message="Run /pay-invoice 1",
                                         triggered_by="loop")
    execution.status, execution.error = "skipped", "Not run: held"
    if held:
        db.held[execution.id] = {"request_id": REQUEST_ID}
    db.start_loop_run(loop["id"], 1, execution_id=execution.id)
    return loop["id"], execution.id


class TestLoop:
    @pytest.mark.parametrize("on_failure", ["abort", "continue"])
    def test_a_held_iteration_stops_the_loop_approval_required_not_failed(self, loop_env, on_failure):
        """Not a task failure (it must not count against `on_failure`), and not
        a reason to run iteration 2 — which would raise another approval for
        the same template."""
        ls, db, calls, _script = loop_env
        lid, eid = _loop_with_closed_run(db, held=True, on_failure=on_failure)

        assert _drain(ls.LoopService().advance_on_terminal(eid)) is True

        loop = db.get_loop(lid)
        assert (loop["status"], loop["stop_reason"]) == ("stopped", "approval_required")
        assert calls == []
        # The held run reads `skipped`, never `failed`, and the loop counts no failure.
        assert [r["status"] for r in db.list_loop_runs(lid)] == ["skipped"]
        assert loop["failed_runs"] == 0

    def test_a_skipped_iteration_with_no_gate_record_follows_the_failure_policy(self, loop_env):
        ls, db, calls, _script = loop_env
        lid, eid = _loop_with_closed_run(db, held=False, on_failure="abort")

        _drain(ls.LoopService().advance_on_terminal(eid))

        loop = db.get_loop(lid)
        assert (loop["status"], loop["stop_reason"]) == ("failed", "error")
        assert [r["status"] for r in db.list_loop_runs(lid)] == ["failed"]

    def test_a_gated_dispatch_is_scanned_on_the_rendered_message_and_never_written_failed(self, loop_env):
        """The gate reads the RENDERED iteration — what an approval would run and
        what the approver sees (an approved raw template would run with its
        placeholders unrendered). A raise is not a crash — the row is
        already SKIPPED — so the advance runs and nothing writes FAILED."""
        ls, db, calls, script = loop_env
        script["gate"] = True
        template = "Run /pay-invoice for batch {{run}} after: {{previous_response}}"

        lid = _drain(ls.LoopService().start_loop(
            agent_name=AGENT, message_template=template, max_runs=3, on_failure="continue"))["id"]

        assert len(calls) == 1
        assert "request_text" not in calls[0]          # the gate reads `message` itself
        assert calls[0]["message"] == "Run /pay-invoice for batch 1 after: "
        assert [s for _e, s in db.status_writes if s == "failed"] == []
        loop = db.get_loop(lid)
        assert (loop["status"], loop["stop_reason"]) == ("stopped", "approval_required")


# ---------------------------------------------------------------------------
# 4. routers/paid.py
# ---------------------------------------------------------------------------


async def _drive_paid(monkeypatch, adapter, exc):
    """`paid_chat` with verify passing (the `_drive_paid` of
    `test_3114_pull_route_callers`), the dispatch raising `exc`."""
    import routers.paid as paid

    config = SimpleNamespace(enabled=True, nvm_environment="testnet", credits_per_request=1)
    db = MagicMock()
    db.get_nevermined_config_with_key.return_value = {"config": config, "nvm_api_key": "k"}
    db.get_public_channel_model.return_value = None
    payment = MagicMock(
        verify_payment=AsyncMock(return_value=SimpleNamespace(
            success=True, payer="0xp", agent_request_id="r1", error=None)),
        settle_payment_once=AsyncMock(return_value=SimpleNamespace(
            success=True, tx_hash="0xt", remaining_balance=1, error=None)),
    )
    idem = MagicMock()
    idem.begin.return_value = MagicMock(replay=False)
    monkeypatch.setattr(paid, "NEVERMINED_AVAILABLE", True)
    monkeypatch.setattr(paid, "db", db)
    monkeypatch.setattr(paid, "idempotency_service", idem)
    monkeypatch.setattr(paid, "get_nevermined_payment_service", lambda: payment)
    monkeypatch.setattr(paid, "get_task_execution_service", lambda: object())
    monkeypatch.setattr(paid, "build_public_channel_caller_prompt", lambda *a, **k: None)
    adapter.side_effect = exc

    resp = await paid.paid_chat(
        AGENT, SimpleNamespace(message="/pay-invoice 100 EUR", session_id=None),
        SimpleNamespace(headers={"payment-signature": "tok"}, base_url="http://localhost/"))
    return resp, json.loads(bytes(resp.body).decode()), payment, idem, db


class TestPaid:
    @pytest.mark.asyncio
    async def test_an_approval_answers_202_settles_nothing_and_releases_the_claim(
            self, monkeypatch, adapter):
        resp, body, payment, idem, db = await _drive_paid(monkeypatch, adapter, _pending())

        assert resp.status_code == 202
        assert (body["status"], body["request_id"]) == ("pending_approval", REQUEST_ID)
        assert body["message"].startswith("Not run:")
        assert body["payment"]["settled"] is False
        payment.settle_payment_once.assert_not_called()
        idem.fail.assert_called_once_with(idem.begin.return_value)
        idem.complete.assert_not_called()
        assert db.log_nevermined_payment.call_args.kwargs["error"] == "Not run: approval_pending"

    @pytest.mark.asyncio
    async def test_a_refusal_answers_its_own_status_and_code_unsettled(self, monkeypatch, adapter):
        resp, body, payment, idem, _db = await _drive_paid(monkeypatch, adapter, _refused(429))

        assert resp.status_code == 429
        assert body["detail"]["code"] == "rate_limited"
        assert body["detail"]["status"] == "refused"
        assert body["payment"]["settled"] is False
        payment.settle_payment_once.assert_not_called()
        idem.fail.assert_called_once_with(idem.begin.return_value)


# ---------------------------------------------------------------------------
# 5. routers/a2a.py
# ---------------------------------------------------------------------------


@pytest.fixture
def a2a_client(monkeypatch):
    """The `test_157_a2a_inbound_server` client: a fixed principal, an exposed
    agent, the dispatch raising, and an idempotency double that records."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import routers.a2a as a2a
    from services import a2a_gate

    monkeypatch.setattr(a2a, "db", SimpleNamespace(
        get_a2a_exposed=lambda name: name == AGENT,
        can_user_access_agent=lambda user, name: name == AGENT))
    dispatch = AsyncMock(side_effect=_pending())
    monkeypatch.setattr(a2a, "dispatch_and_await_terminal", dispatch)

    class _Audit:
        async def log(self, **kwargs):
            return None

    monkeypatch.setattr(a2a, "platform_audit_service", _Audit())
    idem = MagicMock()
    idem.begin.return_value = SimpleNamespace(replay=False, in_flight=False, snapshot=None,
                                              key="m-1", enabled=True)
    monkeypatch.setattr(a2a, "idempotency_service", idem)
    a2a_gate.clear_provider()

    app = FastAPI()
    app.include_router(a2a.a2a_server_router)
    user = SimpleNamespace(id=1, username="alice", email="alice@example.com", role="user",
                           agent_name=None, mcp_key_id="k1")
    # The dependency object the route itself holds — `dependencies` is re-imported
    # between tests (conftest pop list), so a fresh import would not match.
    app.dependency_overrides[a2a.get_current_user] = lambda: user
    return SimpleNamespace(http=TestClient(app), dispatch=dispatch, idem=idem)


def _a2a(client, method):
    r = client.http.post(f"/a2a/{AGENT}", json={
        "jsonrpc": "2.0", "id": 7, "method": method,
        "params": {"message": {"role": "user", "messageId": "m-1",
                               "parts": [{"kind": "text", "text": "/pay-invoice 100 EUR"}]}}})
    assert r.status_code == 200
    if method == "message/stream":
        events = [json.loads(line[len("data: "):]) for line in r.text.splitlines()
                  if line.startswith("data: ")]
        return events[-1]["result"]
    return r.json()["result"]


class TestA2A:
    @pytest.mark.parametrize("method", ["message/send", "message/stream"])
    def test_an_approval_is_an_input_required_task_keyed_on_the_request(self, a2a_client, method):
        task = _a2a(a2a_client, method)

        assert task["status"]["state"] == "input-required"
        assert task["id"] == REQUEST_ID
        assert task["artifacts"][0]["parts"][0]["text"] == NOTICE
        # Completed, not failed: a retried messageId replays the same ask.
        _decision, eid, snapshot = a2a_client.idem.complete.call_args.args
        assert eid == REQUEST_ID
        assert snapshot["status"]["state"] == "input-required"
        a2a_client.idem.fail.assert_not_called()
        if method == "message/stream":
            assert task["final"] is True

    @pytest.mark.parametrize("method", ["message/send", "message/stream"])
    def test_a_refusal_is_a_rejected_task_and_releases_the_key(self, a2a_client, method):
        a2a_client.dispatch.side_effect = _refused(403, "approval_not_available_here")

        task = _a2a(a2a_client, method)

        assert task["status"]["state"] == "rejected"
        assert "Too many approval requests" in task["status"]["message"]["parts"][0]["text"]
        a2a_client.idem.fail.assert_called_once()
        a2a_client.idem.complete.assert_not_called()


# ---------------------------------------------------------------------------
# 6. services/public_chat_service.py
# ---------------------------------------------------------------------------


def _public(monkeypatch):
    """The `_public` harness of `test_3114_pull_route_callers`."""
    from services import public_chat_service as pcs

    db = MagicMock()
    db.get_access_policy.return_value = {}
    db.count_recent_messages_by_ip.return_value = 0
    db.count_recent_messages_by_token.return_value = 0
    db.get_or_create_public_chat_session.return_value = SimpleNamespace(id="cs-7")
    db.build_public_chat_context.return_value = "Previous conversation: …\n\nCurrent message: …"
    db.get_public_chat_session.return_value = SimpleNamespace(message_count=2)
    db.get_public_channel_model.return_value = None
    db.create_task_execution.return_value = SimpleNamespace(id="e-async")
    monkeypatch.setattr(pcs, "db", db)
    monkeypatch.setattr(pcs, "get_agent_container", lambda n: SimpleNamespace(status="running"))
    monkeypatch.setattr(pcs, "build_public_channel_caller_prompt", lambda *a, **k: None)
    return pcs, db


def _public_request(message="/pay-invoice 100 EUR", async_mode=False):
    return SimpleNamespace(session_token=None, session_id="anon-1", files=None,
                           message=message, async_mode=async_mode)


def _assistant_turns(db):
    return [c.kwargs for c in db.add_public_chat_message.call_args_list
            if c.kwargs.get("role") == "assistant"]


class TestPublicChat:
    @pytest.mark.asyncio
    async def test_a_sync_turn_answers_with_the_notice_and_stores_it(self, monkeypatch, adapter):
        pcs, db = _public(monkeypatch)

        out = await pcs.run_public_chat({"id": "link-1", "agent_name": AGENT},
                                        _public_request(), "1.2.3.4")

        assert out.response == NOTICE and out.response.startswith("Not run:")
        assert _assistant_turns(db) == [dict(session_id="cs-7", role="assistant", content=NOTICE,
                                             cost=None, sender_email=None)]
        kw = adapter.await_args.kwargs
        assert kw["request_text"] == "/pay-invoice 100 EUR"
        assert kw["message"] == db.build_public_chat_context.return_value

    @pytest.mark.asyncio
    async def test_an_async_turn_hands_the_visitors_words_to_the_background(
            self, monkeypatch, adapter):
        pcs, db = _public(monkeypatch)

        out = await pcs.run_public_chat({"id": "link-1", "agent_name": AGENT},
                                        _public_request(async_mode=True), "1.2.3.4")
        for _ in range(50):
            if adapter.await_count:
                break
            await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert out["status"] == "accepted"
        assert adapter.await_args.kwargs["request_text"] == "/pay-invoice 100 EUR"
        assert [t["content"] for t in _assistant_turns(db)] == [NOTICE]

    @pytest.mark.asyncio
    async def test_the_background_turn_stores_the_notice_and_returns(self, monkeypatch, adapter):
        pcs, db = _public(monkeypatch)

        await pcs._execute_public_chat_background(
            agent_name=AGENT, context_prompt="ctx", source_email="v@example.com",
            execution_id="e1", chat_session_id="cs-9", session_identifier="v@example.com",
            identifier_type="email", verified_email="v@example.com",
            request_text="/pay-invoice 5 EUR")

        assert adapter.await_args.kwargs["request_text"] == "/pay-invoice 5 EUR"
        assert _assistant_turns(db) == [dict(session_id="cs-9", role="assistant", content=NOTICE,
                                             sender_email="v@example.com")]


# ---------------------------------------------------------------------------
# 7. adapters/message_router.py
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_channel_message_held_by_the_gate_is_answered_in_the_conversation(
        monkeypatch, adapter):
    """The `_router_stamp` harness of `test_ent549_file_audience`, with the
    adapter patched where the router's lazy import finds it."""
    import adapters.message_router as mr

    monkeypatch.setattr(mr, "get_task_execution_service", lambda: MagicMock())
    monkeypatch.setattr(mr, "build_public_channel_caller_prompt", lambda *a, **kw: None)
    monkeypatch.setattr(mr, "build_voice_capability_prompt", lambda *a, **kw: None)
    monkeypatch.setattr(mr, "_get_channel_allowed_tools", lambda: ["WebSearch"])
    monkeypatch.setattr(mr.db, "get_public_channel_model", lambda agent: None)
    channel = MagicMock()
    channel.get_source_identifier.return_value = "telegram:9001:424242"
    channel.send_response = AsyncMock()
    channel.indicate_done = AsyncMock()
    message = SimpleNamespace(text="/pay-invoice 100 EUR", channel_id="424242", thread_id=None,
                              files=[], metadata={})
    router = mr.ChannelMessageRouter.__new__(mr.ChannelMessageRouter)

    out = await router._run_agent_task(
        channel, message, AGENT, "bot-token", "telegram", False,
        container=None, upload_dir=None,
        context_prompt="Sender: ada\nHistory: earlier someone said hi\n\n/pay-invoice 100 EUR",
        verified_email=None, image_data=[])

    assert out is None
    channel.send_response.assert_awaited_once()
    assert channel.send_response.await_args.args[1].text == NOTICE
    kw = adapter.await_args.kwargs
    assert kw["request_text"] == "/pay-invoice 100 EUR"
    assert kw["message"].startswith("Sender: ada")


# ---------------------------------------------------------------------------
# 8. services/gemini_voice.py
# ---------------------------------------------------------------------------


def _voice(monkeypatch, gates):
    """`_execute_tool` with the real `enforce` over a stubbed gate map, and the
    agent client patched where the tool's lazy import finds it."""
    import services.agent_client as agent_client
    import services.skill_gate_service as sgs
    from services import gemini_voice as gv

    monkeypatch.setattr(sgs, "list_skill_gates", lambda agent: gates)
    monkeypatch.setattr(sgs.role_addressing, "owner_email", lambda agent: "owner@example.com")
    client = MagicMock()
    client.task = AsyncMock(return_value=SimpleNamespace(response_text="done"))
    get_client = MagicMock(return_value=client)
    monkeypatch.setattr(agent_client, "get_agent_client", get_client)
    return gv.GeminiVoiceService.__new__(gv.GeminiVoiceService), client, get_client, sgs


class TestVoiceTool:
    def test_a_gated_prompt_is_refused_and_never_reaches_the_agent(self, monkeypatch):
        import services.skill_gate_service as sgs

        svc, client, get_client, _ = _voice(monkeypatch, {"pay-invoice": sgs.SkillGate()})

        out = asyncio.run(svc._execute_tool(AGENT, "run_task",
                                            {"prompt": "please run /pay-invoice 100 EUR"}))

        assert "pay-invoice" in out and "can't be requested from here" in out
        get_client.assert_not_called()
        client.task.assert_not_awaited()

    def test_an_ungated_prompt_reaches_the_agent(self, monkeypatch):
        svc, client, get_client, _ = _voice(monkeypatch, {})

        out = asyncio.run(svc._execute_tool(AGENT, "run_task",
                                            {"prompt": "please run /pay-invoice 100 EUR"}))

        assert out == "done"
        get_client.assert_called_once_with(AGENT)
        client.task.assert_awaited_once_with("please run /pay-invoice 100 EUR", timeout=28.0)


# ---------------------------------------------------------------------------
# 9. routers/internal.py
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exc", [_pending, _refused], ids=["approval", "refusal"])
def test_internal_background_does_not_write_failed_over_a_held_row(db_backend, monkeypatch, exc):
    """The backstop closed the row SKIPPED before raising. The generic handler
    would try to write FAILED over it (SKIPPED is not in its "already terminal"
    tuple); the row-level CAS happens to refuse that write too, so the attempt —
    not only the row — is what pins the caller's own branch."""
    import routers.internal as internal
    from models import InternalTaskExecutionRequest

    db = internal.db
    row = db.create_task_execution(agent_name=AGENT, message="Run /pay-invoice",
                                   triggered_by="schedule")
    _hrun("UPDATE schedule_executions SET status = 'skipped', error = :e WHERE id = :i",
          e=f"Not run: approval {REQUEST_ID} pending", i=row.id)
    writes: list = []
    real_write = db.update_execution_status

    def _spy(*args, **kwargs):
        writes.append(kwargs.get("status"))
        return real_write(*args, **kwargs)

    monkeypatch.setattr(db, "update_execution_status", _spy)
    svc = SimpleNamespace(execute_task=AsyncMock(side_effect=exc()))

    asyncio.run(internal._execute_task_internal_background(
        svc, InternalTaskExecutionRequest(agent_name=AGENT, message="Run /pay-invoice",
                                          execution_id=row.id)))

    svc.execute_task.assert_awaited_once()
    assert writes == []
    assert _scalar("SELECT status FROM schedule_executions WHERE id = :i", i=row.id) == "skipped"


# ---------------------------------------------------------------------------
# 10. shared_sessions/service.py — a room turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_room_turn_held_by_the_gate_says_why_in_the_room(monkeypatch, adapter):
    """The room-wake harness of `test_3114_pull_route_callers`."""
    from shared_sessions import service

    room = "room-1"
    monkeypatch.delenv("PULL_MODE_PILOT_AGENTS", raising=False)
    delta = [
        {"seq": 4, "sender_kind": "user", "sender_identity": "c@example.com",
         "content": f"@{AGENT} please /pay-invoice 100 EUR", "kind": "message"},
        {"seq": 5, "sender_kind": "system", "sender_identity": None,
         "content": "scout joined the room", "kind": "system"},
        {"seq": 6, "sender_kind": "agent", "sender_identity": "scout",
         "content": "I can't pay it myself.", "kind": "message"},
    ]
    monkeypatch.setattr(service.db, "get_participant",
                        lambda *a, **k: {"last_read_seq": 3, "cached_session_id": "s1"})
    monkeypatch.setattr(service.db, "get_room", lambda *a, **k: {
        "id": room, "name": "Room", "status": "open", "topic": None})
    monkeypatch.setattr(service.db, "get_messages", lambda *a, **k: delta)
    monkeypatch.setattr(service.db, "list_participants", lambda _r: [
        {"kind": "agent", "identity": AGENT, "left_at": None}])
    monkeypatch.setattr(service.db, "advance_read_cursor", lambda *a, **k: None)
    for name in ("_mark_agent_working", "_clear_agent_working", "_broadcast"):
        monkeypatch.setattr(service, name, lambda *a, **k: None)
    monkeypatch.setattr(service, "_room_inbox_context", AsyncMock(return_value=("", [])))
    posted: list = []
    monkeypatch.setattr(service, "_post_system",
                        lambda room_id, content: posted.append((room_id, content)))
    post_message = AsyncMock(return_value={})
    monkeypatch.setattr(service, "post_message", post_message)

    await service._wake_agent(SimpleNamespace(email="c@example.com"), room, AGENT, 0)

    assert posted == [(room, NOTICE)]
    post_message.assert_not_called()
    kw = adapter.await_args.kwargs
    # The participants' own lines since the agent last spoke — system lines and
    # the transcript scaffolding around them are not a request anyone made.
    assert kw["request_text"] == f"@{AGENT} please /pay-invoice 100 EUR\nI can't pay it myself."
    assert kw["message"] != kw["request_text"]


# ---------------------------------------------------------------------------
# 11. client_portal/service.py — a Workspace turn
# ---------------------------------------------------------------------------
# The ent#403 portal-turn harness (`_failed_turn` installs every collaborator a
# turn reaches), reused as `test_3012_model_rejection` does so the mock stack
# never forks. Only the turn runner is replaced: it records what it was given
# and raises what the gate would.

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ent403_workspace_model import EMAIL as _P_EMAIL, SESSION as _P_SESSION  # noqa: E402
from test_ent403_workspace_model import _failed_turn, svc  # noqa: E402,F401 — `svc` is a fixture


def _portal_turn_raising(svc, monkeypatch, exc):
    _failed_turn(svc, monkeypatch, code=None)
    seen: dict = {}

    async def _run_turn(*a, **kw):
        seen.update(kw)
        raise exc

    monkeypatch.setattr(svc, "_run_sync_turn_and_clear_marker", _run_turn)
    return seen


QUOTE = ("[Client Portal] The user is replying to your earlier message in this "
         "conversation:\n> /pay-invoice 100 EUR\n\n")


class TestPortal:
    def test_a_held_turn_is_answered_with_the_notice_never_failed(self, svc, monkeypatch):
        """Eyeball finding: a non-retryable portal error renders as a Failed turn
        (`PortalConversation.vue` terminal card). Nothing failed — the request is
        waiting — so the turn answers with the notice, persisted like any reply."""
        from client_portal import db as portal_db

        _portal_turn_raising(svc, monkeypatch, _pending())
        written: list = []
        monkeypatch.setattr(portal_db, "add_portal_message",
                            lambda *a, **kw: written.append(a))
        out = asyncio.run(svc.portal_chat(AGENT, "/pay-invoice 100 EUR", _P_EMAIL,
                                          session_id=_P_SESSION))

        assert out["response"] == NOTICE
        assert [(w[3], w[4]) for w in written] == [("assistant", NOTICE)]
        assert out["message_id"]

    def test_a_refusal_keeps_its_own_status_and_is_named_gated(self, svc, monkeypatch):
        from client_portal.service import ClientPortalError

        _portal_turn_raising(svc, monkeypatch, _refused(429, "approval_queue_full"))
        with pytest.raises(ClientPortalError) as info:
            asyncio.run(svc.portal_chat(AGENT, "/pay-invoice 1", _P_EMAIL, session_id=_P_SESSION))

        assert (info.value.status_code, info.value.category) == (429, "gated")
        assert info.value.retryable is False
        assert info.value.category in svc.PORTAL_FAILURE_CATEGORIES

    def test_the_gate_reads_the_quoted_message_and_what_was_typed_only(self, svc, monkeypatch):
        """Review C2: a reply quoting a held `/pay-invoice` hands the agent the
        invocation again; the gate must read the quote, and must not read the
        history, canvas or manifest scaffolding around the turn."""
        from client_portal import db as portal_db

        seen = _portal_turn_raising(svc, monkeypatch, _pending())
        monkeypatch.setattr(portal_db, "add_portal_message", lambda *a, **kw: None)
        asyncio.run(svc.portal_chat(AGENT, "yes, do that", _P_EMAIL, session_id=_P_SESSION,
                                    reply_context=QUOTE))

        assert seen["request_text"] == QUOTE + "yes, do that"
        assert seen["message"].endswith(QUOTE + "yes, do that")


    @pytest.mark.parametrize("flag", [True, False])
    def test_the_turn_carries_whether_the_workspace_proved_a_person(self, svc, monkeypatch, flag):
        """Only a proven person may run their own gated request (self-approval);
        the Workspace route knows (`PortalPrincipal.is_person`), the turn passes it
        on. Unset (voice relays a model's paraphrase) means unproven."""
        from client_portal.service import ClientPortalError

        seen = _portal_turn_raising(svc, monkeypatch, _refused())
        with pytest.raises(ClientPortalError):
            asyncio.run(svc.portal_chat(AGENT, "hi", _P_EMAIL, session_id=_P_SESSION,
                                        gate_is_person=flag))
        req = seen["gate_requester"]
        assert (req.kind, req.email, req.is_person) == ("person", _P_EMAIL, flag)

    def test_an_unset_flag_is_unproven(self, svc, monkeypatch):
        from client_portal.service import ClientPortalError

        seen = _portal_turn_raising(svc, monkeypatch, _refused())
        with pytest.raises(ClientPortalError):
            asyncio.run(svc.portal_chat(AGENT, "hi", _P_EMAIL, session_id=_P_SESSION))
        assert seen["gate_requester"].is_person is False


from test_2320_portal_failed_turn_visibility import _drain as _drain_turn  # noqa: E402
from test_2320_portal_failed_turn_visibility import redis_stub, streaming  # noqa: E402,F401


@pytest.mark.parametrize("flag", [True, False])
def test_the_streaming_turn_forwards_the_proven_person(streaming, monkeypatch, flag):
    svc, state = streaming
    seen: list = []

    async def _fake_chat(*a, **kw):
        seen.append(kw)
        return {"response": "done", "cost": 0.0, "session_id": kw.get("session_id")}

    monkeypatch.setattr(svc, "portal_chat", _fake_chat)
    asyncio.run(_drain_turn(svc.start_portal_turn(AGENT, "hi", _P_EMAIL, _P_SESSION,
                                                  gate_is_person=flag)))
    assert seen[0]["gate_is_person"] is flag


def _principal(is_person):
    from client_portal.portal_auth import PortalPrincipal
    return PortalPrincipal(_P_EMAIL, True, is_person)


def _stub_route(monkeypatch):
    from client_portal import router as portal_router
    from services import rate_limiter

    monkeypatch.setattr(portal_router, "_require_roster", lambda *a, **k: None)
    monkeypatch.setattr(rate_limiter, "enforce", lambda *a, **k: None)
    monkeypatch.setattr(portal_router.service, "validate_requested_model", lambda m, **k: None)
    monkeypatch.setattr(portal_router.service, "reply_context", lambda *a, **k: "")
    monkeypatch.setattr(portal_router.service, "validated_open_canvas", lambda *a, **k: None)
    return portal_router


@pytest.mark.parametrize("flag", [True, False])
def test_both_workspace_routes_pass_the_principals_person_fact(monkeypatch, flag):
    """The sync route and the streaming route both carry it — a flag honoured by
    one path comes back exactly when the other is taken (the ent#555 lesson)."""
    from starlette.requests import Request
    from client_portal.models import PortalChatRequest

    portal_router = _stub_route(monkeypatch)
    calls: dict = {}

    async def _chat(*a, **kw):
        calls["sync"] = kw
        return {"response": "ok", "cost": 0.0, "session_id": "s1", "message_id": None}

    async def _start(*a, **kw):
        calls["stream"] = kw
        return {"execution_id": "e1", "session_id": "s1", "wait_budget_seconds": 60}

    monkeypatch.setattr(portal_router.service, "portal_chat", _chat)
    monkeypatch.setattr(portal_router.service, "start_portal_turn", _start)
    body = PortalChatRequest(message="/pay-invoice 1")
    asyncio.run(portal_router.portal_chat(AGENT, body, principal=_principal(flag)))
    request = Request({"type": "http", "headers": [], "method": "POST", "path": "/"})
    asyncio.run(portal_router.portal_chat_stream(AGENT, body, request, principal=_principal(flag)))
    assert calls["sync"]["gate_is_person"] is flag
    assert calls["stream"]["gate_is_person"] is flag


# ---------------------------------------------------------------------------
# 12. services/channel_completion_report.py — the approved run's reply
# ---------------------------------------------------------------------------
# The ent#457 end-to-end driver: the real `report_completion` against stubbed
# edges (the row, the portal DB, the effect guard).

from test_ent457_portal_completion_report import (  # noqa: E402
    _SESSION as _REPORT_SESSION, _Row as _ReportRow, _drive as _drive_report)


def _gate_record_for(monkeypatch, record):
    import database

    def _read(eid):
        if isinstance(record, Exception):
            raise record
        return record
    monkeypatch.setattr(database.db, "get_gate_request_by_dispatched_execution", _read, raising=False)


def test_an_approved_runs_reply_reaches_the_thread_it_came_from(monkeypatch):
    """Eyeball finding: an approved run keeps the requester's trigger
    (`public` for the Workspace), which the reporter reads as "already replied
    inline". Nobody was waiting on it, so its result is reported."""
    from services import channel_completion_report as ccr
    _gate_record_for(monkeypatch, {"request_id": "gate-x", "state": "dispatched"})
    ok, written = _drive_report(ccr, monkeypatch, _ReportRow(triggered_by="public"),
                                session=_REPORT_SESSION)
    assert ok is True and len(written) == 1


@pytest.mark.parametrize("record", [None, RuntimeError("db down")], ids=["turn", "unreadable"])
def test_the_turns_own_reply_is_still_never_reported_twice(monkeypatch, record):
    from services import channel_completion_report as ccr
    _gate_record_for(monkeypatch, record)
    ok, written = _drive_report(ccr, monkeypatch, _ReportRow(triggered_by="public"),
                                session=_REPORT_SESSION)
    assert ok is False and written == []
