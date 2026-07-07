"""
Regression tests for #1472 — stale `next_run_at` (enabled schedule shows
"Next: Nd ago"). Each test drives the REAL `SchedulerService` against a
temp-SQLite `SchedulerDatabase` (conftest fixtures).

Coverage map:
  A1  autonomy-off skip advances next_run_at ONLY (no row, no last_run_at)
  A2  every fire outcome (success / dispatched / failure / exception) advances
      next_run_at exactly once, at fire time
  A3  a schedule whose _add_job() fails TRANSIENTLY is retried; a PERMANENT
      failure (bad timezone / bad cron) is bounded (not retried every sync);
      _add_job returns a bool
"""

import sys
import sqlite3
from pathlib import Path

_src = str(Path(__file__).resolve().parents[2] / "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytz

from scheduler.service import SchedulerService
from scheduler.database import SchedulerDatabase
from scheduler.models import ExecutionStatus

PAST = datetime(2020, 1, 1, 4, 0, 0)  # unambiguously in the past


def _is_future(dt):
    """next_run_at is stored tz-aware; compare correctly against UTC now."""
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt > datetime.now(timezone.utc)


def _insert(db_path, sid, agent, *, cron="0 4 * * *", enabled=1,
            next_run_at=PAST, autonomy=1, timezone="UTC"):
    now = datetime.utcnow().isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO agent_ownership(agent_name,owner_id,autonomy_enabled,created_at)"
        " VALUES(?,?,?,?)", (agent, 1, autonomy, now))
    conn.execute(
        "INSERT INTO agent_schedules(id,agent_name,name,cron_expression,message,enabled,"
        "timezone,owner_id,created_at,updated_at,next_run_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (sid, agent, "Sched", cron, "do it", enabled, timezone, 1, now, now,
         next_run_at.isoformat()))
    conn.commit()
    conn.close()


def _exec_count(db_path, sid):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM schedule_executions WHERE schedule_id=?", (sid,)
        ).fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def svc(initialized_db, mock_lock_manager):
    s = SchedulerService(
        database=SchedulerDatabase(database_path=initialized_db),
        lock_manager=mock_lock_manager,
    )
    s._redis = MagicMock()  # _publish_event must not hit real Redis
    return s


# --------------------------------------------------------------------------- A1
@pytest.mark.asyncio
async def test_a1_autonomy_skip_advances_next_only(svc, initialized_db):
    """Autonomy-off: advance next_run_at, but write NO row and NO last_run_at."""
    _insert(initialized_db, "s1", "a1", autonomy=0, next_run_at=PAST)
    await svc._execute_schedule_with_lock("s1", triggered_by="schedule")
    after = svc.db.get_schedule("s1")
    assert _is_future(after.next_run_at), "autonomy-skip left next_run_at stale"
    assert after.last_run_at is None, "autonomy-skip must not touch last_run_at (nothing ran)"
    assert _exec_count(initialized_db, "s1") == 0, "autonomy-skip must not write an execution row"


# --------------------------------------------------------------------------- A2
@pytest.mark.asyncio
async def test_a2_failed_dispatch_advances_next_run(svc, initialized_db):
    _insert(initialized_db, "s2", "a2", next_run_at=PAST)
    with patch.object(svc, "_run_pre_check", new=AsyncMock(return_value=None)), \
         patch.object(svc, "_call_backend_execute_task",
                      new=AsyncMock(return_value={"status": "failed", "error": "boom"})):
        await svc._execute_schedule_with_lock("s2", triggered_by="schedule")
    assert _is_future(svc.db.get_schedule("s2").next_run_at), "failed run left next_run_at stale"


@pytest.mark.asyncio
async def test_a2_dispatch_exception_advances_next_run(svc, initialized_db):
    """A raised dispatch (timeout / connection error) must still advance."""
    _insert(initialized_db, "s3", "a3", next_run_at=PAST)
    with patch.object(svc, "_run_pre_check", new=AsyncMock(return_value=None)), \
         patch.object(svc, "_call_backend_execute_task",
                      new=AsyncMock(side_effect=Exception("dispatch timeout"))):
        await svc._execute_schedule_with_lock("s3", triggered_by="schedule")
    assert _is_future(svc.db.get_schedule("s3").next_run_at), "exception path left next_run_at stale"


@pytest.mark.asyncio
async def test_a2_success_advances_once(svc, initialized_db):
    """Regression: success still ends with next_run_at advanced after the inline
    success-branch advance was removed."""
    _insert(initialized_db, "s4", "a4", next_run_at=PAST)
    with patch.object(svc, "_run_pre_check", new=AsyncMock(return_value=None)), \
         patch.object(svc, "_call_backend_execute_task",
                      new=AsyncMock(return_value={"status": ExecutionStatus.SUCCESS})):
        await svc._execute_schedule_with_lock("s4", triggered_by="schedule")
    after = svc.db.get_schedule("s4")
    assert _is_future(after.next_run_at), "success left next_run_at stale"
    assert after.last_run_at is not None, "a real run must record last_run_at"


# --------------------------------------------------------------------------- A3
@pytest.mark.asyncio
async def test_a3_transient_add_failure_retried(svc, initialized_db):
    """A transient _add_job failure must be retried on the next sync."""
    svc.initialize()
    try:
        _insert(initialized_db, "s5", "a5", next_run_at=PAST)  # valid cron + tz
        n = {"c": 0}
        real = svc.scheduler.add_job

        def flaky(*a, **k):
            n["c"] += 1
            if n["c"] == 1:
                raise RuntimeError("transient hiccup")
            return real(*a, **k)

        with patch.object(svc.scheduler, "add_job", side_effect=flaky):
            await svc._sync_agent_schedules()   # attempt 1 → transient fail
            await svc._sync_agent_schedules()   # attempt 2 → must retry
        assert n["c"] >= 2, "transient add-failure was not retried (orphaned — path 4)"
        assert svc.scheduler.get_job("schedule_s5") is not None, "schedule never registered after retry"
    finally:
        svc.shutdown()


@pytest.mark.asyncio
async def test_a3_permanent_add_failure_bounded(svc, initialized_db):
    """A permanent _add_job failure (invalid timezone) must NOT be retried every
    sync — otherwise A3 becomes a forever error-log storm."""
    svc.initialize()
    try:
        _insert(initialized_db, "s6", "a6", next_run_at=PAST, timezone="Not/AZone")
        with patch.object(svc, "_add_job", wraps=svc._add_job) as spy:
            await svc._sync_agent_schedules()
            await svc._sync_agent_schedules()
            await svc._sync_agent_schedules()
        assert spy.call_count <= 1, (
            f"permanent (bad-tz) add-failure retried {spy.call_count}x — log-storm (A3a)")
    finally:
        svc.shutdown()


@pytest.mark.asyncio
async def test_a3_add_job_returns_bool(svc, initialized_db):
    svc.initialize()
    try:
        _insert(initialized_db, "s7", "a7")
        assert svc._add_job(svc.db.get_schedule("s7")) is True
        conn = sqlite3.connect(initialized_db)
        conn.execute("UPDATE agent_schedules SET timezone='Not/AZone' WHERE id='s7'")
        conn.commit()
        conn.close()
        assert svc._add_job(svc.db.get_schedule("s7")) is False
    finally:
        svc.shutdown()


# --------------------------------------------------------------------------- A4
@pytest.mark.asyncio
async def test_a4_missed_detection_non_utc(svc, initialized_db):
    """A non-UTC (Europe/Kiev) schedule missed 30m ago must be detected. The old
    code stripped tzinfo without converting, so a +03:00 next_run_at looked 3h in
    the future and the missed window was silently skipped."""
    kiev = pytz.timezone("Europe/Kiev")
    now_utc = datetime.now(pytz.utc)
    missed_next = (now_utc - timedelta(minutes=30)).astimezone(kiev)  # 30m ago, Kiev-rendered
    _insert(initialized_db, "s8", "a8", timezone="Europe/Kiev", next_run_at=missed_next)
    conn = sqlite3.connect(initialized_db)
    conn.execute("UPDATE agent_schedules SET last_run_at=? WHERE id='s8'",
                 ((now_utc - timedelta(hours=2)).isoformat(),))
    conn.commit()
    conn.close()
    missed = svc._get_missed_schedules([svc.db.get_schedule("s8")])
    assert [m.id for m in missed] == ["s8"], "non-UTC missed window not detected (tz-strip bug — A4)"


@pytest.mark.asyncio
async def test_a2_failed_run_not_recaught_as_missed(svc, initialized_db):
    """Phantom-catch-up fix: after a failed fire (fire-time advance moves
    next_run_at to the future + sets last_run_at), _get_missed_schedules must NOT
    re-fire that already-run-and-failed window on a restart."""
    _insert(initialized_db, "s9", "a9", next_run_at=PAST)
    with patch.object(svc, "_run_pre_check", new=AsyncMock(return_value=None)), \
         patch.object(svc, "_call_backend_execute_task",
                      new=AsyncMock(return_value={"status": "failed", "error": "boom"})):
        await svc._execute_schedule_with_lock("s9", triggered_by="schedule")
    missed = svc._get_missed_schedules([svc.db.get_schedule("s9")])
    assert missed == [], "failed run re-caught as a missed window (phantom catch-up)"
