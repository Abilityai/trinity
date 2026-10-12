"""#3589 — a watchdog-claimed turn records the agent's finish time, not the
claim time.

Before: `update_execution_status` (and `complete_activity`) stamped
`completed_at = now` unconditionally and derived `duration_ms` from it. Every
live writer finalizes within seconds of the turn's end, so "now" was the turn's
end. The #2944 claim path is the exception: it runs ≥
`RETAINED_RESULT_CLAIM_GRACE_SECONDS` (180 s) plus up to one cleanup cycle
(300 s) after the agent retained the terminal, so a 64 s turn was recorded as
5 m 33 s on the execution row AND the dispatch activity — a fabricated duration
on a recovery path, the #45 / #767 / #1804 class — while the transcript footer
on the same page said 1 m 4 s. The #1083 result callback has the same shape
(it retries up to the slot-lease deadline and a startup sweep re-POSTs after a
restart), so the fix is on the wire contract, not on the claim alone.

Now the producer's terminal timestamp rides `ExecutionResultEnvelope.completed_at`
(wire) → `terminal_from_callback_payload` → `TerminalEnvelope.completed_at` →
`apply_result` → `ExecutionResult.completed_at` / `complete_activity(completed_at=)`
→ the two writers, which use it instead of the clock when given. The claim
path fills it from the record's `retained_at`; every other caller leaves it
`None` and keeps today's behaviour. Each link is pinned here:

  * helper   — `terminal_completed_at`: None → now, naive → UTC, future → now;
  * writers  — the given instant is what the row records and `duration_ms` is
               measured to it (real database via `db_harness`, both tables);
  * mapper   — `terminal_from_callback_payload` carries the field, None by default;
  * applier  — `apply_result` threads `envelope.completed_at` into the execution
               write AND the activity close on BOTH branches;
  * claimer  — `_claim_retained_result` sets it from `retained_at`, and a value
               it cannot read degrades to "writer stamps now", never to a
               refused claim or a crashed sweep.

Harness note: `services.*` are imported INSIDE each test (the
`test_2944_watchdog_claims_result.py` shape — the unit conftest pops them
between collection and test).
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun, scalar as _hscalar, seed_execution, seed_schedule  # noqa: E402,F401
from unit._write_params import flat_kwargs  # noqa: E402

pytestmark = pytest.mark.unit

# The reported case (2026-10-12 re-run, issue body): the agent retained the
# terminal at 09:51:51Z with metadata.duration_ms = 64234; the watchdog claimed
# it 267 s later and the row recorded duration_ms = 332721.
_TURN_MS = 64_234
_CLAIM_DELAY_S = 267
_T = datetime(2026, 10, 12, 9, 51, 51, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Helper — one rule set for both writers
# ---------------------------------------------------------------------------

class TestTerminalCompletedAtHelper:
    def test_none_is_now(self):
        from utils.helpers import terminal_completed_at

        before = _now()
        got = terminal_completed_at(None)
        assert before <= got <= _now()
        assert got.tzinfo is not None

    def test_a_past_instant_is_kept_verbatim(self):
        from utils.helpers import terminal_completed_at

        assert terminal_completed_at(_T) == _T

    def test_naive_is_utc(self):
        from utils.helpers import terminal_completed_at

        assert terminal_completed_at(_T.replace(tzinfo=None)) == _T

    def test_a_future_instant_is_clamped_to_now(self):
        """A producer's clock ahead of ours must not bill a turn as longer than
        the wall clock has run (the clamp is in the writer so every producer of
        a terminal timestamp inherits it)."""
        from utils.helpers import terminal_completed_at

        future = _now() + timedelta(minutes=5)
        got = terminal_completed_at(future)
        assert got < future
        assert got <= _now()


# ---------------------------------------------------------------------------
# Writers — the row records the given instant
# ---------------------------------------------------------------------------

@pytest.fixture
def ops(db_backend):
    for mod in ("db.connection", "db.schedules", "db.activities", "database"):
        sys.modules.pop(mod, None)
    from db.activities import ActivityOperations
    from db.schedules import ScheduleOperations

    return SimpleNamespace(
        sched=ScheduleOperations(user_ops=MagicMock(), agent_ops=MagicMock()),
        act=ActivityOperations(),
    )


def _running_row(started_at: datetime) -> str:
    seed_schedule("sched-3589")
    return seed_execution(
        "sched-3589", "agent-a", exec_id="exec-3589", started_at=_iso(started_at),
        status="running", duration_ms=None, cost=None, triggered_by="manual",
    )


def _exec_row(eid: str) -> tuple:
    return (
        _hscalar("SELECT completed_at FROM schedule_executions WHERE id = :i", i=eid),
        _hscalar("SELECT duration_ms FROM schedule_executions WHERE id = :i", i=eid),
    )


def _open_activity(started_at: datetime) -> str:
    _hrun(
        "INSERT INTO agent_activities (id, agent_name, activity_type, activity_state, "
        "started_at, related_execution_id, created_at, triggered_by) "
        "VALUES ('act-3589', 'agent-a', 'chat_start', 'started', :sa, 'exec-3589', :sa, 'user')",
        sa=_iso(started_at),
    )
    return "act-3589"


def _act_row(aid: str) -> tuple:
    return (
        _hscalar("SELECT completed_at FROM agent_activities WHERE id = :i", i=aid),
        _hscalar("SELECT duration_ms FROM agent_activities WHERE id = :i", i=aid),
    )


class TestExecutionWriter:
    def test_the_reported_case_records_the_turn_not_the_claim(self, ops):
        """started_at = dispatch, completed_at = the agent's retained_at, and the
        write happens 267 s later: the row must say 64 s, not 332 s."""
        from db.write_params import ExecutionResult

        retained_at = _now() - timedelta(seconds=_CLAIM_DELAY_S)
        started_at = retained_at - timedelta(milliseconds=_TURN_MS)
        eid = _running_row(started_at)

        assert ops.sched.update_execution_status(
            eid, "success", ExecutionResult(response="done", completed_at=retained_at)
        ) is True
        completed_at, duration_ms = _exec_row(eid)
        assert completed_at == _iso(retained_at)
        # ±1: duration_ms_between truncates (int(64.234 s * 1000) == 64233).
        assert abs(duration_ms - _TURN_MS) <= 1

    def test_no_completed_at_still_stamps_now(self, ops):
        """Every existing caller passes nothing and keeps today's behaviour."""
        from db.write_params import ExecutionResult
        from utils.helpers import parse_iso_timestamp

        eid = _running_row(_now() - timedelta(seconds=10))
        before = _now()
        assert ops.sched.update_execution_status(eid, "success", ExecutionResult(response="x")) is True
        completed_at, duration_ms = _exec_row(eid)
        assert before - timedelta(seconds=1) <= parse_iso_timestamp(completed_at) <= _now() + timedelta(seconds=1)
        assert 9_000 <= duration_ms <= 12_000

    def test_a_naive_completed_at_is_utc_on_the_failed_arm(self, ops):
        from db.write_params import ExecutionResult

        started_at = _now() - timedelta(seconds=300)
        eid = _running_row(started_at)
        naive_end = (started_at + timedelta(seconds=10)).replace(tzinfo=None)
        assert ops.sched.update_execution_status(
            eid, "failed", ExecutionResult(error="boom", completed_at=naive_end)
        ) is True
        completed_at, duration_ms = _exec_row(eid)
        assert completed_at == _iso(started_at + timedelta(seconds=10))
        assert duration_ms == 10_000

    def test_a_completed_at_before_started_at_clamps_like_1832(self, ops):
        from db.write_params import ExecutionResult

        started_at = _now() - timedelta(seconds=60)
        eid = _running_row(started_at)
        assert ops.sched.update_execution_status(
            eid, "success", ExecutionResult(response="x", completed_at=started_at - timedelta(seconds=5))
        ) is True
        assert _exec_row(eid)[1] == 0

    def test_a_future_completed_at_is_clamped_to_now(self, ops):
        from db.write_params import ExecutionResult
        from utils.helpers import parse_iso_timestamp

        eid = _running_row(_now() - timedelta(seconds=60))
        assert ops.sched.update_execution_status(
            eid, "success", ExecutionResult(response="x", completed_at=_now() + timedelta(hours=1))
        ) is True
        completed_at, duration_ms = _exec_row(eid)
        assert parse_iso_timestamp(completed_at) <= _now() + timedelta(seconds=1)
        assert duration_ms <= 62_000


class TestActivityWriter:
    def test_the_activity_close_records_the_given_instant(self, ops):
        from models import ActivityCloseOutcome

        retained_at = _now() - timedelta(seconds=_CLAIM_DELAY_S)
        started_at = retained_at - timedelta(milliseconds=_TURN_MS)
        aid = _open_activity(started_at)
        assert ops.act.complete_activity(aid, "completed", completed_at=retained_at) is ActivityCloseOutcome.UPDATED
        completed_at, duration_ms = _act_row(aid)
        assert completed_at == _iso(retained_at)
        assert abs(duration_ms - _TURN_MS) <= 1  # int() truncation, see above

    def test_no_completed_at_still_stamps_now(self, ops):
        from models import ActivityCloseOutcome
        from utils.helpers import parse_iso_timestamp

        aid = _open_activity(_now() - timedelta(seconds=10))
        before = _now()
        assert ops.act.complete_activity(aid, "completed") is ActivityCloseOutcome.UPDATED
        completed_at, duration_ms = _act_row(aid)
        assert before - timedelta(seconds=1) <= parse_iso_timestamp(completed_at) <= _now() + timedelta(seconds=1)
        assert 9_000 <= duration_ms <= 12_000

    def test_a_future_completed_at_is_clamped_to_now(self, ops):
        from models import ActivityCloseOutcome
        from utils.helpers import parse_iso_timestamp

        aid = _open_activity(_now() - timedelta(seconds=60))
        assert ops.act.complete_activity(
            aid, "failed", error="x", completed_at=_now() + timedelta(hours=1)
        ) is ActivityCloseOutcome.UPDATED
        completed_at, duration_ms = _act_row(aid)
        assert parse_iso_timestamp(completed_at) <= _now() + timedelta(seconds=1)
        assert duration_ms <= 62_000


# ---------------------------------------------------------------------------
# Mapper — the wire contract carries the field
# ---------------------------------------------------------------------------

class TestCallbackMapper:
    def test_default_is_none_so_the_writer_stamps_now(self):
        from models import ExecutionResultEnvelope
        from services.execution_envelope import terminal_from_callback_payload

        env = terminal_from_callback_payload(ExecutionResultEnvelope(status="success", response="ok"), "e1")
        assert env.completed_at is None

    @pytest.mark.parametrize("raw", ["2026-10-12T09:51:51Z", "2026-10-12T09:51:51+00:00",
                                     "2026-10-12T09:51:51", _T, _T.replace(tzinfo=None)])
    def test_the_field_is_carried_as_an_aware_utc_datetime(self, raw):
        from models import ExecutionResultEnvelope
        from services.execution_envelope import terminal_from_callback_payload

        payload = ExecutionResultEnvelope(status="failed", error="boom", completed_at=raw)
        env = terminal_from_callback_payload(payload, "e1")
        assert env.completed_at == _T
        assert env.completed_at.tzinfo is not None

    def test_garbage_is_a_clean_422_shape_at_the_contract(self):
        """Bounded at the contract like `execution_time_ms` (#2434): the
        endpoint answers 422, the claim never builds one from garbage (below)."""
        from pydantic import ValidationError
        from models import ExecutionResultEnvelope

        with pytest.raises(ValidationError):
            ExecutionResultEnvelope(status="success", completed_at="not-a-time")


# ---------------------------------------------------------------------------
# Applier — apply_result threads envelope.completed_at into both closes
# ---------------------------------------------------------------------------

def _run_apply(envelope, *, cas_won=True):
    from services.task_execution_service import TaskExecutionService

    mock_db = MagicMock()
    mock_db.update_execution_status.return_value = cas_won
    reconciled = MagicMock()
    reconciled.status = "cancelled"
    mock_db.get_execution.return_value = reconciled
    mock_activity = MagicMock(complete_activity=AsyncMock())
    with (
        patch("services.task_execution_service.db", mock_db),
        patch("services.task_execution_service.get_capacity_manager",
              return_value=MagicMock(release=AsyncMock())),
        patch("services.task_execution_service.activity_service", mock_activity),
        patch("services.task_execution_service._record_dispatch_terminal", AsyncMock()),
    ):
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                TaskExecutionService().apply_result("agent-a", envelope, activity_id="act-1")
            )
        finally:
            loop.close()
    return mock_db, mock_activity


def _envelope(status_name: str, **over):
    from services.task_execution_service import TaskExecutionStatus, TerminalEnvelope

    base = dict(execution_id="exec-3589", status=getattr(TaskExecutionStatus, status_name),
                metadata={"cost_usd": 0.01, "context_window": 200000})
    base.update(over)
    return TerminalEnvelope(**base)


class TestApplyResultThreadsCompletedAt:
    @pytest.mark.parametrize("status_name,fields", [
        ("SUCCESS", {"response": "ok"}),
        ("FAILED", {"error": "boom"}),
    ])
    def test_both_branches_pass_the_timestamp_to_both_writers(self, status_name, fields):
        mdb, mact = _run_apply(_envelope(status_name, completed_at=_T, **fields))
        assert flat_kwargs(mdb.update_execution_status.call_args)["completed_at"] == _T
        assert mact.complete_activity.await_args.kwargs["completed_at"] == _T

    @pytest.mark.parametrize("status_name,fields", [
        ("SUCCESS", {"response": "ok"}),
        ("FAILED", {"error": "boom"}),
    ])
    def test_no_timestamp_leaves_both_writers_to_stamp_now(self, status_name, fields):
        mdb, mact = _run_apply(_envelope(status_name, **fields))
        assert flat_kwargs(mdb.update_execution_status.call_args)["completed_at"] is None
        assert mact.complete_activity.await_args.kwargs.get("completed_at") is None


# ---------------------------------------------------------------------------
# Claimer — _claim_retained_result fills completed_at from retained_at
# ---------------------------------------------------------------------------

def _cs():
    from services import cleanup_service as cs  # noqa: WPS433 — lazy on purpose

    return cs


def _record(*, age=600, status="success", **over):
    base = {
        "status": status, "response": "done" if status == "success" else None,
        "error": None if status == "success" else "boom", "error_code": None,
        "terminal_reason": "completed" if status == "success" else "error",
        "metadata": {"cost_usd": 0.03, "session_id": "sess-1", "duration_ms": _TURN_MS},
        "execution_log": [{"type": "result"}], "session_id": "sess-1",
        "retained_at": (_now() - timedelta(seconds=age)).isoformat(),
        "truncated_log": False,
    }
    base.update(over)
    return base


def _claim(record):
    """Run the claim with apply_result captured. Returns (verdict, envelope)."""
    cs = _cs()
    apply_result = AsyncMock(return_value=SimpleNamespace(status=record["status"], error_code=None))
    svc = MagicMock(apply_result=apply_result)
    with (
        patch.object(cs, "db") as mock_db,
        patch("services.task_execution_service.get_task_execution_service", return_value=svc),
        patch("services.task_execution_service.dispatch_breaker_active", return_value=True),
    ):
        mock_db.get_open_activity_id_for_execution.return_value = "act-1"
        verdict = asyncio.run(cs._claim_retained_result("agent-a", "exec-3589", record))
    envelope = apply_result.await_args.args[1] if apply_result.await_args else None
    return verdict, envelope


class TestClaimCarriesTheRetainedTimestamp:
    @pytest.mark.parametrize("status", ["success", "failed"])
    def test_completed_at_is_the_records_retained_at(self, status):
        from utils.helpers import parse_iso_timestamp

        record = _record(status=status)
        verdict, envelope = _claim(record)
        assert verdict == "claimed"
        assert envelope.completed_at == parse_iso_timestamp(record["retained_at"])
        assert envelope.completed_at.tzinfo is not None

    def test_the_callback_inputs_are_otherwise_unchanged(self):
        """No `metadata.duration_ms` → `execution_time_ms` mapping: the claim
        hands `apply_result` EXACTLY the result-callback's inputs (#2944), and
        `execution_time_ms` means "how long the caller waited", not the CLI's
        own run time."""
        _, envelope = _claim(_record())
        assert envelope.execution_time_ms is None

    @pytest.mark.parametrize("retained_at", ["not-a-time", None, "", 1760262711.0])
    def test_an_unreadable_retained_at_leaves_the_writer_to_stamp_now(self, retained_at):
        """`_retained_age_seconds` already treats an unreadable stamp as +inf
        (claimable). The claim must not refuse such a record — and must not
        crash the sweep on `int(inf)` after the CAS already won."""
        verdict, envelope = _claim(_record(retained_at=retained_at))
        assert verdict == "claimed"
        assert envelope.completed_at is None

    @pytest.mark.parametrize("bad", ["64234", 64234.5, True, -1, 2**31])
    def test_an_out_of_contract_execution_time_ms_is_dropped_not_fatal(self, bad):
        """The envelope bounds `execution_time_ms` to int4 (#2434). A record
        carrying a value outside it must not raise inside the claim — that
        maps to an `error` verdict and withholds a FINISHED turn every cycle
        until the 120-minute sweep fails it."""
        verdict, envelope = _claim(_record(execution_time_ms=bad))
        assert verdict == "claimed"
        assert envelope.execution_time_ms is None

    def test_an_in_contract_execution_time_ms_is_kept(self):
        _, envelope = _claim(_record(execution_time_ms=1234))
        assert envelope.execution_time_ms == 1234

    def test_age_and_timestamp_read_the_same_field_the_same_way(self):
        """One parser for `retained_at`: the grace check and the terminal
        timestamp must never disagree on Z vs +00:00 vs naive vs non-string."""
        cs = _cs()

        for raw in ("2026-10-12T09:51:51Z", "2026-10-12T09:51:51+00:00", "2026-10-12T09:51:51"):
            assert cs._retained_at({"retained_at": raw}) == _T
            assert cs._retained_age_seconds({"retained_at": raw}) > 0
        for raw in ("garbage", None, 1760262711.0, ""):
            assert cs._retained_at({"retained_at": raw}) is None
            assert cs._retained_age_seconds({"retained_at": raw}) == float("inf")
