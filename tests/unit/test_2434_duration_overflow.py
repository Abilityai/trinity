"""#2434 — a fabricated ``duration_ms`` must never wedge a watchdog sweep.

``duration_ms`` is a SQLAlchemy ``Integer`` — PostgreSQL ``int4``, ceiling
``2**31 - 1`` ms = **24.855 days**. Every watchdog sweep closed a stale row with
a fabricated ``duration_ms = now - started_at``. Past that ceiling the ``UPDATE``
raises ``psycopg2.errors.NumericValueOutOfRange``.

**The blast radius is the transaction, not the row.** Each sweep runs its SELECT
*and* its whole per-row UPDATE loop inside one ``with get_engine().begin()``, so
on PostgreSQL the first overflow aborts the transaction, every later
``conn.execute`` raises ``InFailedSqlTransaction``, and ``begin()`` rolls back
rows already updated in that batch. While one >=24.8-day row exists, no stale
execution and no stale activity anywhere in the fleet is ever closed — every
cycle, forever — and each sweep reports itself complete.

Three properties of this file are load-bearing; changing any of them makes it
stop proving anything.

1. **It calls the DB-ops layer, never the service layer.**
   ``db/schedules/cleanup.py`` and ``db/activities.py`` have no ``try/except``,
   so the ops call raises. ``cleanup_service`` catches ``Exception`` at every
   sweep and returns ``[]`` / ``0`` / ``False``, so a service-layer test passes
   on **pre-fix** code.

2. **Every case asserts the writer's own return value before touching
   ``duration_ms``.** ``duration_ms IS NULL`` is trivially satisfied by a row the
   sweep never selected, and two sweeps have narrow predicates that make that
   easy to hit by accident: ``mark_no_session_executions_failed`` requires
   ``claude_session_id IS NULL OR = ''`` (the nearest seed helper in the repo
   sets ``'dispatched'``, which is silently excluded), and
   ``close_open_activities_for_executions`` requires
   ``activity_type IN (chat_start, schedule_start)``.

3. **Case 1 seeds TWO rows.** One 33-day row and one healthy 10-minute row in the
   same batch. Pre-fix, on real PostgreSQL, BOTH stay ``running`` — that is the
   actual defect. A single-row seed would still pass against a "fix" that merely
   caught the exception per row and left the healthy rows unclosed.

**Pre-fix this file is RED on BOTH backends, for different reasons — and that is
a good property, not a broken harness.** ``[postgres]`` fails with
``NumericValueOutOfRange``; ``[sqlite]`` fails the ``duration_ms IS NULL``
assertion, because SQLite stores an 8-byte int and happily persists
``2851200000``. Only ``[postgres]`` proves the *reported* defect, which is why
``schema-parity.yml`` now exports ``TEST_POSTGRES_URL``: a SQLite-only run
cannot fail on the overflow at all.

The source-level half of this fix lives in ``test_1832_duration_clamp.py``
(the extended AST guard); the mirror contract in
``test_1713_scheduler_utils_parity.py``.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Bootstrap: mirror test_1832_duration_clamp.py so `src/backend` imports resolve.
# ---------------------------------------------------------------------------
_THIS = Path(__file__).resolve()
_ROOT = _THIS.parent.parent.parent
_BACKEND = _ROOT / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun, scalar as _hscalar  # noqa: E402,F401

_DB_MODULES = ("db.connection", "db.schedules", "db.activities", "database")
# The snapshot/restore pair `tests/lint_sys_modules.py` recognises (precedent:
# test_1832_duration_clamp.py). The db.* modules must be evicted so each fixture
# rebinds them to the backend db_backend just activated, and restored afterwards
# so a later test in the same session does not inherit this file's engine.
#
# `models` is deliberately NOT in this list: `ActivityCloseOutcome` is compared
# by IDENTITY below, and it lives in `models` precisely so that an evicted `db.*`
# cannot make that comparison silently go False at full-suite scale (#1804).
_STUBBED_MODULE_NAMES = list(_DB_MODULES)

AGENT = "agent-2434"


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value

# The reported reproduction: a real restore whose newest execution was 33 days
# old (~2.85 bn ms). 24.855 days is the ceiling, so 33 days clears it with room.
_OVERFLOW_DAYS = 33
# Comfortably past every sweep's window, comfortably inside the int4 ceiling.
_HEALTHY_STALE_SECONDS = 10 * 60


@pytest.fixture
def ops(db_backend):
    """Composed ``ScheduleOperations`` bound to a fresh production schema."""
    for _m in _DB_MODULES:
        sys.modules.pop(_m, None)
    from db.schedules import ScheduleOperations

    yield ScheduleOperations(user_ops=MagicMock(), agent_ops=MagicMock())
    for _m in _DB_MODULES:
        sys.modules.pop(_m, None)


@pytest.fixture
def act_ops(db_backend):
    """``ActivityOperations`` bound to the same fresh schema."""
    for _m in _DB_MODULES:
        sys.modules.pop(_m, None)
    from db.activities import ActivityOperations

    yield ActivityOperations()
    for _m in _DB_MODULES:
        sys.modules.pop(_m, None)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _ago(*, days: int = 0, seconds: int = 0) -> str:
    return _iso(datetime.now(timezone.utc) - timedelta(days=days, seconds=seconds))


def _seed_execution(
    exec_id: str,
    *,
    started_at: str,
    status: str = "running",
    claude_session_id: str | None = "dispatched",
) -> str:
    """Seed a `schedule_executions` row.

    Deliberately NOT reusing ``test_1771c_schedules_cas_edges.py::insert_execution``:
    its #2243 default anchors ``started_at`` to *now* precisely so durations stay
    bounded, which would make every case here pass both pre- and post-fix.
    """
    cols = {
        "id": exec_id,
        "schedule_id": "sched-2434",
        "agent_name": AGENT,
        "status": status,
        "started_at": started_at,
        "message": "do the thing",
        "triggered_by": "schedule",
        "claude_session_id": claude_session_id,
    }
    names = ", ".join(cols)
    binds = ", ".join(f":{k}" for k in cols)
    _hrun(f"INSERT INTO schedule_executions ({names}) VALUES ({binds})", **cols)
    return exec_id


def _seed_activity(
    act_id: str,
    *,
    started_at: str,
    activity_type: str = "chat_start",
    state: str = "started",
    related_execution_id: str | None = None,
) -> str:
    cols = {
        "id": act_id,
        "agent_name": AGENT,
        "activity_type": activity_type,
        "activity_state": state,
        "started_at": started_at,
        "triggered_by": "schedule",
        "related_execution_id": related_execution_id,
        "created_at": started_at,
    }
    names = ", ".join(cols)
    binds = ", ".join(f":{k}" for k in cols)
    _hrun(f"INSERT INTO agent_activities ({names}) VALUES ({binds})", **cols)
    return act_id


def _exec_row(exec_id: str) -> tuple:
    from sqlalchemy import text
    from db.engine import get_engine

    with get_engine().connect() as conn:
        return tuple(
            conn.execute(
                text(
                    "SELECT status, duration_ms FROM schedule_executions "
                    "WHERE id = :i"
                ),
                {"i": exec_id},
            ).first()
        )


def _activity_row(act_id: str) -> tuple:
    from sqlalchemy import text
    from db.engine import get_engine

    with get_engine().connect() as conn:
        return tuple(
            conn.execute(
                text(
                    "SELECT activity_state, duration_ms FROM agent_activities "
                    "WHERE id = :i"
                ),
                {"i": act_id},
            ).first()
        )


# ---------------------------------------------------------------------------
# 1. Batch integrity — the real unit of the defect
# ---------------------------------------------------------------------------


def test_2434_one_overflowing_row_does_not_wedge_the_whole_batch(ops):
    """The reported failure, stated as the property that actually broke.

    Pre-fix on PostgreSQL the 33-day row raises mid-loop, aborting the shared
    transaction; ``begin()`` then rolls back the healthy row that had already
    been updated, so BOTH stay ``running`` and the sweep closes nothing — every
    cycle, forever, for every agent on the instance.
    """
    poison = _seed_execution("e2434-poison", started_at=_ago(days=_OVERFLOW_DAYS))
    healthy = _seed_execution(
        "e2434-healthy", started_at=_ago(seconds=_HEALTHY_STALE_SECONDS)
    )

    # timeout_minutes=5 => both rows are past the 300s stale window.
    closed = ops.mark_stale_executions_failed(timeout_minutes=5)

    assert closed == 2, "the sweep must close BOTH rows, not abort the batch"
    for eid in (poison, healthy):
        status, duration = _exec_row(eid)
        assert status == "failed", eid
        assert duration is None, (eid, duration)


def test_2434_wedge_is_idempotent_second_sweep_finds_nothing(ops):
    """Once closed, the row is gone from the sweep's candidate set.

    The pre-fix symptom was a row re-selected and re-failing forever; the fix is
    only complete if the row actually leaves the working set.
    """
    _seed_execution("e2434-idem", started_at=_ago(days=_OVERFLOW_DAYS))

    assert ops.mark_stale_executions_failed(timeout_minutes=5) == 1
    assert ops.mark_stale_executions_failed(timeout_minutes=5) == 0


# ---------------------------------------------------------------------------
# 2. All four execution sweeps, not just the observed one
# ---------------------------------------------------------------------------


def test_2434_no_session_sweep_closes_an_overflowing_row(ops):
    """``mark_no_session_executions_failed`` — the silent-launch-failure sweep.

    Its predicate requires ``claude_session_id IS NULL OR = ''``; seeding the
    repo's usual ``'dispatched'`` would exclude the row and pass vacuously,
    which is why the return value is asserted first.
    """
    eid = _seed_execution(
        "e2434-nosession",
        started_at=_ago(days=_OVERFLOW_DAYS),
        claude_session_id=None,
    )

    assert ops.mark_no_session_executions_failed(timeout_seconds=60) == 1

    assert _exec_row(eid) == ("failed", None)


def test_2434_stale_slot_reclaim_closes_an_overflowing_row(ops):
    """``fail_stale_slot_execution`` — the #1083 slot reaper's terminal write.

    This one also leaks capacity when it raises: ``cleanup_service`` writes the
    terminal BEFORE releasing the slot, so a raise skips the release and
    ``_reconcile_orphaned_slots`` cannot reclaim a row that is still ``running``.
    """
    eid = _seed_execution("e2434-slot", started_at=_ago(days=_OVERFLOW_DAYS))

    assert ops.fail_stale_slot_execution(eid, "slot reclaimed") is True

    assert _exec_row(eid) == ("failed", None)


def test_2434_watchdog_closes_an_overflowing_row(ops):
    """``mark_execution_failed_by_watchdog`` — the ``[Recovery]`` path.

    This is the exact writer named in the reported log line
    (``[Recovery] Error recovering execution ...: integer out of range``).
    """
    eid = _seed_execution("e2434-watchdog", started_at=_ago(days=_OVERFLOW_DAYS))

    assert ops.mark_execution_failed_by_watchdog(eid, "agent gone") is True

    assert _exec_row(eid) == ("failed", None)


# ---------------------------------------------------------------------------
# 3. The paired activity sweeps — the other half of the reported symptom
# ---------------------------------------------------------------------------


def test_2434_stale_activity_backstop_closes_an_overflowing_row(act_ops):
    """``mark_stale_activities_failed`` — the 120-minute backstop.

    ``[Cleanup] Error marking stale activities`` was its own blocked sweep in the
    report, and a row left ``started`` renders the agent as still working on the
    Dashboard Timeline.
    """
    aid = _seed_activity("a2434-stale", started_at=_ago(days=_OVERFLOW_DAYS))

    assert act_ops.mark_stale_activities_failed(timeout_minutes=30) == 1

    assert _activity_row(aid) == ("failed", None)


def test_2434_bulk_activity_close_handles_an_overflowing_row(act_ops):
    """``close_open_activities_for_executions`` — the #1804 recovery closer.

    Its predicate requires a DISPATCH activity type (``chat_start`` /
    ``schedule_start``); a ``tool_call`` row is deliberately not closed here, so
    the seed type is load-bearing and the return value is asserted first.
    """
    aid = _seed_activity(
        "a2434-bulk",
        started_at=_ago(days=_OVERFLOW_DAYS),
        related_execution_id="e2434-bulk",
    )

    assert act_ops.close_open_activities_for_executions(["e2434-bulk"]) == 1

    assert _activity_row(aid) == ("failed", None)


def test_2434_bulk_activity_close_survives_a_mixed_batch(act_ops):
    """Batch integrity for the activity side: one poison row, one healthy row."""
    poison = _seed_activity(
        "a2434-mix-poison",
        started_at=_ago(days=_OVERFLOW_DAYS),
        related_execution_id="e2434-mix-1",
    )
    healthy = _seed_activity(
        "a2434-mix-healthy",
        started_at=_ago(seconds=_HEALTHY_STALE_SECONDS),
        related_execution_id="e2434-mix-2",
    )

    closed = act_ops.close_open_activities_for_executions(
        ["e2434-mix-1", "e2434-mix-2"]
    )

    assert closed == 2, "one unstorable duration must not roll back the batch"
    for aid in (poison, healthy):
        assert _activity_row(aid) == ("failed", None), aid


# ---------------------------------------------------------------------------
# 4. The MEASURED writers — guarded, but they must not flatten real durations
# ---------------------------------------------------------------------------


def test_2434_terminal_write_succeeds_on_a_row_older_than_the_ceiling(ops):
    """``update_execution_status`` had ``max(0, ...)``, which guards only the LOW
    end — ``max(0, 2_850_000_000)`` is still 2.85 bn."""
    eid = _seed_execution("e2434-terminal", started_at=_ago(days=_OVERFLOW_DAYS))

    assert ops.update_execution_status(eid, "success", response="ok") is True

    assert _exec_row(eid) == ("success", None)


def test_2434_terminal_write_still_records_a_real_duration(ops):
    """The ceiling must not flatten measurements — only unrepresentable ones."""
    eid = _seed_execution("e2434-real", started_at=_ago(seconds=5))

    assert ops.update_execution_status(eid, "success", response="ok") is True

    status, duration = _exec_row(eid)
    assert status == "success"
    assert 4_000 <= duration <= 6_000, duration


def test_2434_complete_activity_succeeds_on_a_row_older_than_the_ceiling(act_ops):
    """``complete_activity`` had no guard at either end."""
    aid = _seed_activity("a2434-complete", started_at=_ago(days=_OVERFLOW_DAYS))

    from models import ActivityCloseOutcome

    outcome = act_ops.complete_activity(aid, status="completed")

    assert outcome is ActivityCloseOutcome.UPDATED
    assert _activity_row(aid) == ("completed", None)


def test_2434_complete_activity_still_records_a_real_duration(act_ops):
    """A normal-age activity keeps its measured duration."""
    aid = _seed_activity("a2434-complete-real", started_at=_ago(seconds=5))

    act_ops.complete_activity(aid, status="completed")

    state, duration = _activity_row(aid)
    assert state == "completed"
    assert 4_000 <= duration <= 6_000, duration


# ---------------------------------------------------------------------------
# 5. The operator-visible acceptance criterion: E-01 becomes clearable
# ---------------------------------------------------------------------------


def _e01_snapshot(*, snap_time: str, started_at: str, running_ids=("e1",)):
    """Build the AgentSnapshot E-01 actually reads.

    E-01 does NOT read terminal rows — it iterates ``snapshot.agents`` ->
    ``running_exec_ids`` / ``running_started_at`` / ``execution_timeout_seconds``.
    Modelled on ``test_canary_invariants.py::TestInvariantE01._snap``, NOT on
    ``terminal_rows``, which E-01 never looks at.
    """
    from canary.snapshot import Snapshot, AgentSnapshot

    return Snapshot(
        snapshot_time=snap_time,
        agents=[
            AgentSnapshot(
                name=AGENT,
                is_system=False,
                max_parallel=3,
                execution_timeout_seconds=900,
                running_exec_ids=set(running_ids),
                running_started_at={eid: started_at for eid in running_ids},
                running_lease_expires_at={eid: None for eid in running_ids},
            )
        ],
    )


def test_2434_e01_is_pinned_critical_by_a_wedged_row_and_clears_once_closed():
    """The invariant the bug pinned permanently red, with no way to clear it.

    E-01 fires on a ``running`` row older than ``execution_timeout_seconds +
    300``. The sweep meant to resolve it was the thing that could not. Once the
    sweep can close the row it leaves ``running_exec_ids`` and E-01 goes green —
    which is what an operator sees, and why "the upgrade restart self-heals" is
    a claim this test backs rather than asserts.
    """
    from canary.invariants import e01_terminal_state_closure as e01

    wedged = _e01_snapshot(
        snap_time="2026-05-18T12:00:00Z", started_at="2026-04-15T12:00:00Z"
    )
    violations = e01.check(wedged)
    assert len(violations) == 1
    assert violations[0].invariant_id == "E-01"
    assert violations[0].severity == "critical"

    # After the sweep closes it, the row is terminal and no longer `running`.
    swept = _e01_snapshot(
        snap_time="2026-05-18T12:00:00Z",
        started_at="2026-04-15T12:00:00Z",
        running_ids=(),
    )
    assert e01.check(swept) == []
