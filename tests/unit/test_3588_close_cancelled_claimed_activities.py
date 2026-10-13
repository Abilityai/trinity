"""#3588 — no activity created or owned by a terminal path stays `started`
after a terminate or a watchdog claim (the #1804 class, fifth occurrence
after #45, #767, #1804, #3589).

Two leaks, both fixed here:

1. **Cancel half.** The three cancel sites (`_cancel_queued_if_queued`,
   `_proxy_terminate_and_finalize`, `_cancel_inflight_if_parked`) create an
   `execution_cancelled` activity via `track_activity` (always STARTED) and
   nothing ever closes it — the 120-minute sweep fabricates its duration.
   The event is a point in time: it is now created and completed in the same
   breath (`_record_cancel_event`), AFTER the real closes, fail-open.
   Site 1 additionally never called the #1804 shared owner at all, so the
   #2329 metadata companions of a queued row leaked too.

2. **Claim half.** `_claim_retained_result` finalizes through `apply_result`,
   which closes only the single dispatch activity id it is handed; the
   caller-side `agent_collaboration` / `self_task` companions (which share
   `related_execution_id`) stayed `started` (`activities_closed=0`). They are
   now closed by `activity_service.close_companion_activities`, which derives
   state AND instant from the PERSISTED row — never from the claim's envelope,
   because `apply_result`'s failure branch returns the same result on a lost
   CAS (only the SUCCESS branch says RECONCILED), so "the claim won" is not
   observable at the call site. Deriving from the row makes a lost
   failure-style claim close companions in the terminal that actually stands.

The invariant asserted throughout is a QUERY, never a call-shape mock: zero
`started` rows for the execution among the activity types a terminal path
owns. Scoped to those types on purpose — `tool_call` rows share the execution
id and have their own closers (#45).

Harness: `db_harness` (real engine, both backends); `services.*` imported
inside each test (the unit conftest pops them between collection and test).
"""
from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
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

pytestmark = pytest.mark.unit

_T = datetime(2026, 10, 13, 9, 30, 0, tzinfo=timezone.utc)

# The activity types a terminal path creates or owns. `tool_call` is excluded
# on purpose: it shares `related_execution_id` but has its own closers (#45),
# and an unscoped count would also pass trivially on an unseeded fixture.
_IN_SCOPE_TYPES = (
    "'chat_start','schedule_start','agent_collaboration','self_task','execution_cancelled'"
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _open_in_scope(eid: str) -> int:
    """THE invariant: no terminal-owned activity stays `started`."""
    return _hscalar(
        "SELECT COUNT(*) FROM agent_activities WHERE related_execution_id = :e "
        f"AND activity_state = 'started' AND activity_type IN ({_IN_SCOPE_TYPES})",
        e=eid,
    )


def _activity(aid: str) -> tuple:
    return (
        _hscalar("SELECT activity_state FROM agent_activities WHERE id = :i", i=aid),
        _hscalar("SELECT completed_at FROM agent_activities WHERE id = :i", i=aid),
        _hscalar("SELECT error FROM agent_activities WHERE id = :i", i=aid),
        _hscalar("SELECT details FROM agent_activities WHERE id = :i", i=aid),
    )


def _event_row(eid: str) -> tuple:
    """(state, completed_at, details) of the execution_cancelled event row."""
    return (
        _hscalar(
            "SELECT activity_state FROM agent_activities WHERE related_execution_id = :e "
            "AND activity_type = 'execution_cancelled'", e=eid),
        _hscalar(
            "SELECT completed_at FROM agent_activities WHERE related_execution_id = :e "
            "AND activity_type = 'execution_cancelled'", e=eid),
        _hscalar(
            "SELECT details FROM agent_activities WHERE related_execution_id = :e "
            "AND activity_type = 'execution_cancelled'", e=eid),
    )


def _seed_exec(eid: str, status: str, *, agent="callee-a", started_at=None,
               completed_at=None, backlog_metadata=None, error=None,
               response=None, duration_ms=None) -> str:
    seed_schedule(f"sched-{eid}")
    seed_execution(
        f"sched-{eid}", agent, exec_id=eid,
        started_at=_iso(started_at or (_now() - timedelta(seconds=90))),
        status=status, duration_ms=duration_ms, cost=None, triggered_by="agent",
    )
    _hrun(
        "UPDATE schedule_executions SET completed_at = :c, backlog_metadata = :m, "
        "error = :err, response = :r WHERE id = :i",
        c=_iso(completed_at) if completed_at else None,
        m=backlog_metadata, err=error, r=response, i=eid,
    )
    return eid


def _seed_activity(aid: str, type_: str, eid: str, *, state="started",
                   agent="caller-a", started_at=None, details=None) -> str:
    _hrun(
        "INSERT INTO agent_activities (id, agent_name, activity_type, activity_state, "
        "started_at, related_execution_id, created_at, triggered_by, details) "
        "VALUES (:i, :a, :t, :s, :sa, :e, :sa, 'agent', :d)",
        i=aid, a=agent, t=type_, s=state,
        sa=_iso(started_at or (_now() - timedelta(seconds=80))),
        e=eid, d=json.dumps(details) if details else None,
    )
    return aid


def _fresh(monkeypatch):
    """Re-resolve db modules a sibling file may have stubbed (3589 shape)."""
    for mod in ("db.connection", "db.schedules", "db.activities", "database"):
        monkeypatch.delitem(sys.modules, mod, raising=False)


# ---------------------------------------------------------------------------
# Cancel half — site 1: cancelled while queued (no dispatch activity exists;
# what leaks are the #2329 metadata companions and the event row itself)
# ---------------------------------------------------------------------------

class TestCancelledWhileQueued:
    def _run(self):
        import services.chat_execution_service as ces

        return asyncio.run(
            ces._cancel_queued_if_queued("callee-a", "exec-q1", "exec-q1", None)
        )

    def _seed(self):
        meta = json.dumps({
            "collaboration_activity_id": "collab-q1",
            "is_self_task": True,
            "self_task_activity_id": "self-q1",
        })
        _seed_exec("exec-q1", "queued", backlog_metadata=meta)
        _seed_activity("collab-q1", "agent_collaboration", "exec-q1", agent="caller-a")
        _seed_activity("self-q1", "self_task", "exec-q1", agent="callee-a")

    def test_no_terminal_owned_activity_stays_started(self, db_backend, monkeypatch):
        _fresh(monkeypatch)
        self._seed()
        assert self._run() == {"status": "cancelled_while_queued", "execution_id": "exec-q1"}
        assert _open_in_scope("exec-q1") == 0

    def test_metadata_companions_close_cancelled(self, db_backend, monkeypatch):
        """A queued row has NO dispatch activity (CHAT_START opens only at
        dispatch, which a queued row never reaches) — the leak is the #2329
        companions the row names, which only `close_execution_activity` finds."""
        _fresh(monkeypatch)
        self._seed()
        self._run()
        assert _activity("collab-q1")[0] == "cancelled"
        assert _activity("self-q1")[0] == "cancelled"

    def test_the_cancel_event_row_is_completed_at_the_terminate_instant(self, db_backend, monkeypatch):
        from utils.helpers import parse_iso_timestamp

        _fresh(monkeypatch)
        self._seed()
        before = _now()
        self._run()
        state, completed_at, details = _event_row("exec-q1")
        assert state == "completed"
        got = parse_iso_timestamp(completed_at)
        if got.tzinfo is None:
            got = got.replace(tzinfo=timezone.utc)
        assert before - timedelta(seconds=1) <= got <= _now() + timedelta(seconds=1)
        assert json.loads(details)["status"] == "cancelled_while_queued"

    def test_an_event_write_failure_never_fails_a_won_cancel(self, db_backend, monkeypatch):
        """The cancel CAS has already won; an activity-write error is logged,
        not raised (H4)."""
        import services.chat_execution_service as ces

        _fresh(monkeypatch)
        self._seed()
        with patch.object(ces.activity_service, "track_activity",
                          AsyncMock(side_effect=RuntimeError("db down"))):
            result = asyncio.run(
                ces._cancel_queued_if_queued("callee-a", "exec-q1", "exec-q1", None)
            )
        assert result == {"status": "cancelled_while_queued", "execution_id": "exec-q1"}
        # The real closes still ran: companions are not left started.
        assert _activity("collab-q1")[0] == "cancelled"


# ---------------------------------------------------------------------------
# Cancel half — site 2: proxy-terminate (running turn)
# ---------------------------------------------------------------------------

def _proxy_client(payload: dict):
    response = MagicMock(status_code=200)
    response.json.return_value = payload
    client = MagicMock(post=AsyncMock(return_value=response))

    @asynccontextmanager
    async def _ctx(name, timeout=None):
        yield client

    return _ctx


class TestProxyTerminate:
    def _run(self, payload):
        import services.chat_execution_service as ces

        capacity = MagicMock(release_if_matches=AsyncMock(return_value=True))
        with (
            patch.object(ces, "agent_httpx_client", _proxy_client(payload)),
            patch.object(ces, "get_capacity_manager", return_value=capacity),
        ):
            return asyncio.run(ces._proxy_terminate_and_finalize(
                "callee-a", "exec-p1", "exec-p1", None
            ))

    def test_terminated_leaves_no_started_activity(self, db_backend, monkeypatch):
        _fresh(monkeypatch)
        _seed_exec("exec-p1", "running")
        _seed_activity("disp-p1", "chat_start", "exec-p1", agent="callee-a")
        result = self._run({"status": "terminated", "returncode": -2})
        assert result["status"] == "terminated"
        assert _open_in_scope("exec-p1") == 0
        assert _activity("disp-p1")[0] == "cancelled"
        state, completed_at, details = _event_row("exec-p1")
        assert state == "completed"
        assert completed_at is not None

    def test_already_finished_still_records_the_attempt_completed(self, db_backend, monkeypatch):
        """Nothing was cancelled (the agent's own terminal stands), but the row
        records the cancel ATTEMPT — completed, labelled by details.status.
        A conscious decision (M3), pinned here."""
        _fresh(monkeypatch)
        _seed_exec("exec-p1", "running")
        result = self._run({"status": "already_finished"})
        assert result["status"] == "already_finished"
        state, _, details = _event_row("exec-p1")
        assert state == "completed"
        assert json.loads(details)["status"] == "already_finished"


# ---------------------------------------------------------------------------
# Cancel half — site 3: cancelled while parked
# ---------------------------------------------------------------------------

class TestCancelledWhileParked:
    def test_parked_cancel_leaves_no_started_activity(self, db_backend, monkeypatch):
        import services.chat_execution_service as ces

        _fresh(monkeypatch)
        _seed_exec("exec-k1", "running")
        _seed_activity("disp-k1", "chat_start", "exec-k1", agent="callee-a")
        capacity = MagicMock(release_if_matches=AsyncMock(return_value=True))
        with (
            patch.object(ces.agent_call_limiter, "cancel_inflight", return_value="parked"),
            patch.object(ces, "get_capacity_manager", return_value=capacity),
        ):
            result = asyncio.run(ces._cancel_inflight_if_parked(
                "callee-a", "exec-k1", "exec-k1", None
            ))
        assert result == {"status": "cancelled_while_parked", "execution_id": "exec-k1"}
        assert _open_in_scope("exec-k1") == 0
        assert _activity("disp-k1")[0] == "cancelled"
        state, completed_at, _ = _event_row("exec-k1")
        assert state == "completed"
        assert completed_at is not None


# ---------------------------------------------------------------------------
# Claim half — close_companion_activities (the service seam)
# ---------------------------------------------------------------------------

class TestCloseCompanionActivities:
    def _close(self, eid):
        from services.activity_service import activity_service

        return asyncio.run(activity_service.close_companion_activities(eid))

    def test_direct_dispatch_companion_closes_from_the_row(self, db_backend, monkeypatch):
        """The issue's repro shape: a fire-and-forget `/task` hop (`parallel_task`,
        chat_execution_service.py:2106) has NO backlog_metadata — the companion is
        reachable only by type + related_execution_id. State and instant come from
        the persisted row."""
        from utils.helpers import parse_iso_timestamp

        _fresh(monkeypatch)
        retained = _T
        _seed_exec("exec-c1", "success", completed_at=retained,
                   response="done", duration_ms=64_000)
        _seed_activity("collab-c1", "agent_collaboration", "exec-c1",
                       details={"action": "parallel_task", "parallel_mode": True})
        assert self._close("exec-c1") == 1
        state, completed_at, error, details = _activity("collab-c1")
        assert state == "completed"
        got = parse_iso_timestamp(completed_at)
        if got.tzinfo is None:
            got = got.replace(tzinfo=timezone.utc)
        assert got == retained
        assert error is None
        d = json.loads(details)
        assert d["execution_id"] == "exec-c1"
        assert d["response_length"] == len("done")

    def test_metadata_named_companions_are_the_belt(self, db_backend, monkeypatch):
        """Union path: ids named in backlog_metadata are closed even if a future
        producer forgets `related_execution_id` — still state-filtered, so an
        already-closed metadata id never inflates the count."""
        _fresh(monkeypatch)
        meta = json.dumps({"collaboration_activity_id": "collab-c2",
                           "is_self_task": True, "self_task_activity_id": "self-c2"})
        _seed_exec("exec-c2", "cancelled", completed_at=_T, backlog_metadata=meta,
                   error="Execution terminated by user")
        _seed_activity("collab-c2", "agent_collaboration", "exec-c2")
        # A self-task row whose producer forgot the execution linkage: only the
        # metadata names it.
        _seed_activity("self-c2", "self_task", None)
        assert self._close("exec-c2") == 2
        assert _activity("collab-c2")[0] == "cancelled"
        assert _activity("self-c2")[0] == "cancelled"

    def test_non_terminal_row_is_a_no_op(self, db_backend, monkeypatch):
        """Re-queue safety: a lease-reaper re-queue means the companions belong
        to the re-delivered attempt (same guard as `_close_queued_activities`)."""
        _fresh(monkeypatch)
        _seed_exec("exec-c3", "running")
        _seed_activity("collab-c3", "agent_collaboration", "exec-c3")
        assert self._close("exec-c3") == 0
        assert _activity("collab-c3")[0] == "started"

    def test_an_authoritative_terminal_upgrades_a_provisional_failed(self, db_backend, monkeypatch):
        """The lookup must agree with the CAS (H1): a SUCCESS terminal's
        COMPLETED close may upgrade a provisionally-FAILED companion, so it must
        be able to FIND it — a started-only lookup makes the upgrade inert."""
        _fresh(monkeypatch)
        _seed_exec("exec-c4", "success", completed_at=_T, response="ok")
        _seed_activity("collab-c4", "agent_collaboration", "exec-c4", state="failed")
        assert self._close("exec-c4") == 1
        assert _activity("collab-c4")[0] == "completed"

    def test_a_provisional_terminal_does_not_touch_a_failed_companion(self, db_backend, monkeypatch):
        _fresh(monkeypatch)
        _seed_exec("exec-c5", "failed", completed_at=_T, error="boom")
        _seed_activity("collab-c5", "agent_collaboration", "exec-c5", state="failed")
        assert self._close("exec-c5") == 0
        assert _activity("collab-c5")[0] == "failed"

    def test_error_is_recorded_only_on_failed(self, db_backend, monkeypatch):
        _fresh(monkeypatch)
        _seed_exec("exec-c6", "failed", completed_at=_T, error="boom")
        _seed_activity("collab-c6", "agent_collaboration", "exec-c6")
        assert self._close("exec-c6") == 1
        state, _, error, _ = _activity("collab-c6")
        assert state == "failed"
        assert error == "boom"

    def test_dispatch_and_event_rows_are_never_companions(self, db_backend, monkeypatch):
        """Codex #8's inverse: the companion close must not touch the dispatch
        activity (apply_result owns it) or the cancel-event row, and must not
        reach another execution's rows."""
        _fresh(monkeypatch)
        _seed_exec("exec-c7", "success", completed_at=_T, response="ok")
        _seed_activity("disp-c7", "chat_start", "exec-c7", agent="callee-a")
        _seed_activity("ev-c7", "execution_cancelled", "exec-c7", agent="callee-a")
        _seed_activity("collab-other", "agent_collaboration", "exec-other")
        assert self._close("exec-c7") == 0
        assert _activity("disp-c7")[0] == "started"
        assert _activity("ev-c7")[0] == "started"
        assert _activity("collab-other")[0] == "started"


# ---------------------------------------------------------------------------
# Claim half — _claim_retained_result wiring
# ---------------------------------------------------------------------------

def _record(*, age=600, status="success", **over):
    base = {
        "status": status, "response": "done" if status == "success" else None,
        "error": None if status == "success" else "boom", "error_code": None,
        "terminal_reason": "completed" if status == "success" else "error",
        "metadata": {"cost_usd": 0.03, "session_id": "sess-1"},
        "execution_log": [{"type": "result"}], "session_id": "sess-1",
        "retained_at": (_now() - timedelta(seconds=age)).isoformat(),
        "truncated_log": False,
    }
    base.update(over)
    return base


def _run_claim(record, eid, *, apply_status=None, error_code=None,
               report=None, stats=None):
    """Drive the real `_claim_retained_result` with `apply_result` mocked (the
    row is pre-seeded in whatever terminal state the real applier would have
    left) and the real activity service beneath it."""
    import services.cleanup_service as cs

    apply_result = AsyncMock(return_value=SimpleNamespace(
        status=apply_status or record["status"], error_code=error_code))
    svc = MagicMock(apply_result=apply_result)
    with (
        patch.object(cs, "db") as mock_db,
        patch("services.task_execution_service.get_task_execution_service", return_value=svc),
        patch("services.task_execution_service.dispatch_breaker_active", return_value=True),
    ):
        mock_db.get_open_activity_id_for_execution.return_value = "act-dispatch"
        verdict = asyncio.run(cs._claim_retained_result(
            "callee-a", eid, record, report=report, stats=stats))
    return verdict


class TestClaimClosesCompanions:
    def test_claimed_turn_closes_the_direct_dispatch_collaboration_row(self, db_backend, monkeypatch):
        """The issue's claim half: inter-agent `/task` hop, watchdog claims the
        retained terminal → the caller-side collaboration row must close, with
        the row's instant (== retained_at via #3589), and the counter must show
        it (`activities_closed=0` was the reported symptom)."""
        from utils.helpers import parse_iso_timestamp

        _fresh(monkeypatch)
        retained = _T
        record = _record(retained_at=_iso(retained))
        # The row as apply_result leaves it: terminal, completed_at = retained_at.
        _seed_exec("exec-w1", "success", completed_at=retained, response="done")
        _seed_activity("collab-w1", "agent_collaboration", "exec-w1",
                       details={"action": "parallel_task"})
        stats: dict = {}
        verdict = _run_claim(record, "exec-w1", stats=stats)
        assert verdict == "claimed"
        state, completed_at, _, _ = _activity("collab-w1")
        assert state == "completed"
        got = parse_iso_timestamp(completed_at)
        if got.tzinfo is None:
            got = got.replace(tzinfo=timezone.utc)
        assert got == retained
        assert stats.get("activities_closed", 0) >= 1
        assert _open_in_scope("exec-w1") == 0

    def test_the_periodic_caller_counts_into_the_report(self, db_backend, monkeypatch):
        """The watchdog cycle passes `report=` and no `stats` — stats-only
        wiring would never show in the live watchdog log (H2)."""
        import services.cleanup_service as cs

        _fresh(monkeypatch)
        _seed_exec("exec-w2", "success", completed_at=_T, response="ok")
        _seed_activity("collab-w2", "agent_collaboration", "exec-w2")
        report = cs.CleanupReport()
        verdict = _run_claim(_record(retained_at=_iso(_T)), "exec-w2", report=report)
        assert verdict == "claimed"
        assert report.activities_closed_on_recovery >= 1

    def test_self_task_and_collaboration_both_close(self, db_backend, monkeypatch):
        _fresh(monkeypatch)
        meta = json.dumps({"collaboration_activity_id": "collab-w3",
                           "is_self_task": True, "self_task_activity_id": "self-w3"})
        _seed_exec("exec-w3", "success", completed_at=_T, response="ok",
                   backlog_metadata=meta)
        _seed_activity("collab-w3", "agent_collaboration", "exec-w3")
        _seed_activity("self-w3", "self_task", "exec-w3", agent="callee-a")
        verdict = _run_claim(_record(retained_at=_iso(_T)), "exec-w3")
        assert verdict == "claimed"
        assert _open_in_scope("exec-w3") == 0

    def test_a_success_style_lost_cas_closes_nothing(self, db_backend, monkeypatch):
        """RECONCILED = the SUCCESS branch lost to another terminal. The claim
        returns `lost` before any companion close."""
        from services.task_execution_service import TaskExecutionErrorCode

        _fresh(monkeypatch)
        _seed_exec("exec-w4", "cancelled", completed_at=_T,
                   error="Execution terminated by user")
        _seed_activity("collab-w4", "agent_collaboration", "exec-w4")
        verdict = _run_claim(_record(retained_at=_iso(_T)), "exec-w4",
                             apply_status="cancelled",
                             error_code=TaskExecutionErrorCode.RECONCILED)
        assert verdict == "lost"
        assert _activity("collab-w4")[0] == "started"

    def test_a_failure_style_lost_cas_closes_in_the_rows_state_never_the_envelopes(self, db_backend, monkeypatch):
        """C1 (found independently by both reviewers): `apply_result`'s failure
        branch returns the SAME result on a lost CAS — only the SUCCESS branch
        says RECONCILED. A FAILED retained record losing to a cancel must not
        stamp companions FAILED + retained_at: the close derives from the
        PERSISTED row, so they close CANCELLED with the canceller's instant —
        and the lattice refuses nothing afterwards."""
        from utils.helpers import parse_iso_timestamp

        _fresh(monkeypatch)
        cancel_instant = _T + timedelta(seconds=30)
        # The cancel won: the row is CANCELLED at the cancel instant, NOT the
        # claim's retained_at.
        _seed_exec("exec-w5", "cancelled", completed_at=cancel_instant,
                   error="Execution terminated by user")
        _seed_activity("collab-w5", "agent_collaboration", "exec-w5")
        record = _record(status="failed", retained_at=_iso(_T))
        verdict = _run_claim(record, "exec-w5", apply_status="failed")
        assert verdict == "claimed"
        state, completed_at, _, _ = _activity("collab-w5")
        assert state == "cancelled", "the ROW's terminal, never the lost envelope's"
        got = parse_iso_timestamp(completed_at)
        if got.tzinfo is None:
            got = got.replace(tzinfo=timezone.utc)
        assert got == cancel_instant

    def test_row_and_activity_agree_when_retained_at_is_unreadable(self, db_backend, monkeypatch):
        """M2: with no readable `retained_at`, the writers stamp now at
        different moments — sourcing the activity instant from the ROW keeps
        the two in agreement by construction."""
        from utils.helpers import parse_iso_timestamp

        _fresh(monkeypatch)
        row_instant = _now() - timedelta(seconds=2)
        _seed_exec("exec-w6", "success", completed_at=row_instant, response="ok")
        _seed_activity("collab-w6", "agent_collaboration", "exec-w6")
        verdict = _run_claim(_record(retained_at="not-a-time"), "exec-w6")
        assert verdict == "claimed"
        row_completed = _hscalar(
            "SELECT completed_at FROM schedule_executions WHERE id = :i", i="exec-w6")
        _, act_completed, _, _ = _activity("collab-w6")
        a = parse_iso_timestamp(act_completed)
        b = parse_iso_timestamp(row_completed)
        assert a.replace(tzinfo=None) == b.replace(tzinfo=None)

    def test_a_companion_close_failure_never_flips_a_claimed_verdict(self, db_backend, monkeypatch):
        import services.cleanup_service as cs
        from services import activity_service as act_mod

        _fresh(monkeypatch)
        _seed_exec("exec-w7", "success", completed_at=_T, response="ok")
        with patch.object(act_mod.activity_service, "close_companion_activities",
                          AsyncMock(side_effect=RuntimeError("boom"))):
            verdict = _run_claim(_record(retained_at=_iso(_T)), "exec-w7")
        assert verdict == "claimed"
