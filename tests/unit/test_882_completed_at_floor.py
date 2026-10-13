"""trinity-enterprise#882 — a terminal `completed_at` is floored at `started_at`.

#3629 threads the producer's terminal instant (the agent-authored `retained_at`,
or the #1083 callback's `completed_at`) into the two terminal writers.
`terminal_completed_at` clamped only the future; a PAST value was written to the
`completed_at` column verbatim. Retention hard-DELETEs executions on
`completed_at < cutoff` (`db/schedules/retention.py`), so an agent that backdates
`retained_at` in its retained-results file gets its own execution row erased on
the next sweep — a self-erasing audit trail. Both producers of the field are
agent-controlled, so the floor lives at the WRITE, not at either claim path.

The floor is a fourth rule inside `terminal_completed_at` ("Three rules, in ONE
place" — now four), as a REQUIRED keyword-only `floor:` parameter: a future
third writer must decide, never silently skip (the `_apply_git_env_from_db`
per-call-site-gate pattern). `started_at` is server-written at admission in both
tables, so the floor floors at a trusted value. Pinned here:

  * helper  — a below-floor instant is raised to the floor; a value within
              `[floor, now]` passes verbatim (the #3589 behaviour this must NOT
              break); the future still clamps to now; the floor wins over the
              clamp when a skewed `started_at` sits in the future;
  * writers — the backdated envelope lands in the `completed_at` COLUMN as
              `started_at` (the column, not just `duration_ms`), the status is
              still terminal, and the in-window / future cases are unchanged —
              real database via `db_harness`, both tables, PG leg when
              `TEST_POSTGRES_URL` is set.

Harness note: `db.*` / `utils.*` are imported INSIDE each test (the
`test_3589_claim_completed_at.py` shape — the unit conftest pops them between
collection and test).
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun, scalar as _hscalar, seed_execution, seed_schedule  # noqa: E402,F401

pytestmark = pytest.mark.unit

# The attack shape from the issue: the agent writes a deeply backdated
# `retained_at` into its retained-results file; the next retention sweep's
# cutoff is far later, so the row is erased.
_BACKDATED = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Helper — the floor rule, beside the three existing rules
# ---------------------------------------------------------------------------

class TestTerminalCompletedAtFloor:
    def test_a_below_floor_instant_is_raised_to_the_floor(self):
        from utils.helpers import terminal_completed_at

        started = _now() - timedelta(seconds=60)
        assert terminal_completed_at(_BACKDATED, floor=started) == started

    def test_a_value_within_the_window_passes_verbatim(self):
        """The #3589 behaviour #3629 exists to deliver — must NOT break."""
        from utils.helpers import terminal_completed_at

        started = _now() - timedelta(seconds=600)
        retained = started + timedelta(seconds=64)
        assert terminal_completed_at(retained, floor=started) == retained

    def test_the_future_still_clamps_to_now(self):
        from utils.helpers import terminal_completed_at

        started = _now() - timedelta(seconds=60)
        got = terminal_completed_at(_now() + timedelta(hours=1), floor=started)
        assert got <= _now()
        assert got >= started

    def test_none_is_now_and_still_floored(self):
        from utils.helpers import terminal_completed_at

        started = _now() - timedelta(seconds=60)
        before = _now()
        got = terminal_completed_at(None, floor=started)
        assert before <= got <= _now()

    def test_the_floor_wins_over_the_clamp_on_skew(self):
        """A skewed server clock can write `started_at` ahead of ours. "Cannot
        end before it starts" is the retention-protecting invariant, so the
        floor applies AFTER the future-clamp; duration stays 0 either way."""
        from utils.helpers import terminal_completed_at

        started = _now() + timedelta(seconds=30)
        assert terminal_completed_at(_BACKDATED, floor=started) == started

    def test_a_naive_floor_is_utc_not_a_crash(self):
        """Same convention as `given`: `aware < naive` raises TypeError, and a
        terminal writer must never crash on the write."""
        from utils.helpers import terminal_completed_at

        started = _now() - timedelta(seconds=60)
        got = terminal_completed_at(_BACKDATED, floor=started.replace(tzinfo=None))
        assert got == started

    def test_floor_none_is_an_explicit_no_floor_decision(self):
        from utils.helpers import terminal_completed_at

        assert terminal_completed_at(_BACKDATED, floor=None) == _BACKDATED

    def test_the_floor_parameter_is_required(self):
        """Keyword-only and required: a future writer cannot silently skip the
        decision — the exact omission class this issue closes."""
        from utils.helpers import terminal_completed_at

        with pytest.raises(TypeError):
            terminal_completed_at(_BACKDATED)


# ---------------------------------------------------------------------------
# Writers — the COLUMN records the floored instant (real DB, both tables)
# ---------------------------------------------------------------------------

@pytest.fixture
def ops(db_backend, monkeypatch):
    for mod in ("db.connection", "db.schedules", "db.activities", "database"):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    from db.activities import ActivityOperations
    from db.schedules import ScheduleOperations

    return SimpleNamespace(
        sched=ScheduleOperations(user_ops=MagicMock(), agent_ops=MagicMock()),
        act=ActivityOperations(),
    )


def _running_row(started_at: datetime) -> str:
    seed_schedule("sched-882")
    return seed_execution(
        "sched-882", "agent-a", exec_id="exec-882", started_at=_iso(started_at),
        status="running", duration_ms=None, cost=None, triggered_by="manual",
    )


def _exec_row(eid: str) -> tuple:
    return (
        _hscalar("SELECT completed_at FROM schedule_executions WHERE id = :i", i=eid),
        _hscalar("SELECT duration_ms FROM schedule_executions WHERE id = :i", i=eid),
        _hscalar("SELECT status FROM schedule_executions WHERE id = :i", i=eid),
    )


def _open_activity(started_at: datetime) -> str:
    _hrun(
        "INSERT INTO agent_activities (id, agent_name, activity_type, activity_state, "
        "started_at, related_execution_id, created_at, triggered_by) "
        "VALUES ('act-882', 'agent-a', 'chat_start', 'started', :sa, 'exec-882', :sa, 'user')",
        sa=_iso(started_at),
    )
    return "act-882"


def _act_row(aid: str) -> tuple:
    return (
        _hscalar("SELECT completed_at FROM agent_activities WHERE id = :i", i=aid),
        _hscalar("SELECT duration_ms FROM agent_activities WHERE id = :i", i=aid),
        _hscalar("SELECT activity_state FROM agent_activities WHERE id = :i", i=aid),
    )


class TestExecutionWriterFloors:
    def test_a_backdated_envelope_lands_as_started_at_in_the_column(self, ops):
        """The retention predicate reads the COLUMN, so the column is what the
        attack targets and what this asserts — not just `duration_ms`."""
        from db.write_params import ExecutionResult

        started_at = _now() - timedelta(seconds=60)
        eid = _running_row(started_at)
        assert ops.sched.update_execution_status(
            eid, "success", ExecutionResult(response="done", completed_at=_BACKDATED)
        ) is True
        completed_at, duration_ms, status = _exec_row(eid)
        assert completed_at == _iso(started_at)
        assert duration_ms == 0
        assert status == "success"  # a floored claim is still a successful claim

    def test_an_in_window_value_is_untouched(self, ops):
        from db.write_params import ExecutionResult

        started_at = _now() - timedelta(seconds=600)
        retained_at = started_at + timedelta(seconds=64)
        eid = _running_row(started_at)
        assert ops.sched.update_execution_status(
            eid, "success", ExecutionResult(response="done", completed_at=retained_at)
        ) is True
        completed_at, duration_ms, _ = _exec_row(eid)
        assert completed_at == _iso(retained_at)
        assert duration_ms == 64_000

    def test_the_future_clamp_is_unchanged(self, ops):
        from db.write_params import ExecutionResult
        from utils.helpers import parse_iso_timestamp

        eid = _running_row(_now() - timedelta(seconds=60))
        assert ops.sched.update_execution_status(
            eid, "success", ExecutionResult(response="x", completed_at=_now() + timedelta(hours=1))
        ) is True
        completed_at, duration_ms, _ = _exec_row(eid)
        assert parse_iso_timestamp(completed_at) <= _now() + timedelta(seconds=1)
        assert duration_ms <= 62_000

    def test_the_failed_arm_floors_too(self, ops):
        """Both producers can report failure; the failed arm writes the same
        column through the same call."""
        from db.write_params import ExecutionResult

        started_at = _now() - timedelta(seconds=60)
        eid = _running_row(started_at)
        assert ops.sched.update_execution_status(
            eid, "failed", ExecutionResult(error="boom", completed_at=_BACKDATED)
        ) is True
        completed_at, duration_ms, status = _exec_row(eid)
        assert completed_at == _iso(started_at)
        assert duration_ms == 0
        assert status == "failed"


class TestActivityWriterFloors:
    def test_a_backdated_close_lands_as_started_at_in_the_column(self, ops):
        from models import ActivityCloseOutcome

        started_at = _now() - timedelta(seconds=60)
        aid = _open_activity(started_at)
        assert ops.act.complete_activity(
            aid, "completed", completed_at=_BACKDATED
        ) is ActivityCloseOutcome.UPDATED
        completed_at, duration_ms, state = _act_row(aid)
        assert completed_at == _iso(started_at)
        assert duration_ms == 0
        assert state == "completed"

    def test_an_in_window_value_is_untouched(self, ops):
        from models import ActivityCloseOutcome

        started_at = _now() - timedelta(seconds=600)
        retained_at = started_at + timedelta(seconds=64)
        aid = _open_activity(started_at)
        assert ops.act.complete_activity(
            aid, "completed", completed_at=retained_at
        ) is ActivityCloseOutcome.UPDATED
        completed_at, duration_ms, _ = _act_row(aid)
        assert completed_at == _iso(retained_at)
        assert duration_ms == 64_000

    def test_the_future_clamp_is_unchanged(self, ops):
        from models import ActivityCloseOutcome
        from utils.helpers import parse_iso_timestamp

        aid = _open_activity(_now() - timedelta(seconds=60))
        assert ops.act.complete_activity(
            aid, "failed", error="x", completed_at=_now() + timedelta(hours=1)
        ) is ActivityCloseOutcome.UPDATED
        completed_at, duration_ms, _ = _act_row(aid)
        assert parse_iso_timestamp(completed_at) <= _now() + timedelta(seconds=1)
        assert duration_ms <= 62_000
