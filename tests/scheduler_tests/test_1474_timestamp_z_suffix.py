"""#1474 — scheduler-written timestamps must serialize as UTC with an explicit 'Z'.

Cron-fired executions were stored by the standalone scheduler with
``datetime.utcnow().isoformat()`` (no 'Z'). In a non-UTC browser JS parsed the
naive string as *local* time, shifting the relative time by the viewer's offset.

These tests pin, at the scheduler DB write boundary:
  * every write site stores a 'Z'-suffixed string (format parity with backend);
  * duration math still works after the write change (the read parser returns
    naive UTC, so ``datetime.utcnow() − started_at`` never becomes aware−naive);
  * reads tolerate legacy naive rows AND offset-bearing (+03:00) rows, preserving
    the UTC *instant*;
  * ``to_utc_iso``/``utc_now_iso`` match the backend ISO-Z format exactly.
"""

import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from scheduler.models import ExecutionStatus
from scheduler.utils import utc_now_iso, to_utc_iso, parse_scheduler_ts

# Backend utc_now_iso() format: "2026-01-15T10:30:00.123456Z"
_ISO_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z$")


def _raw(db, sql, params=()):
    """Read raw stored TEXT values, bypassing the model mappers."""
    conn = sqlite3.connect(db.database_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Write side: every scheduler write stores a 'Z'-suffixed string
# ---------------------------------------------------------------------------

def test_create_execution_started_at_has_z(db):
    ex = db.create_execution("s1", "agent-a", "hello")
    row = _raw(db, "SELECT started_at FROM schedule_executions WHERE id = ?", (ex.id,))
    assert row["started_at"].endswith("Z")
    assert _ISO_Z_RE.match(row["started_at"])


def test_create_skipped_execution_both_ts_have_z(db):
    ex = db.create_skipped_execution("s1", "agent-a", "hello", skip_reason="busy")
    row = _raw(
        db,
        "SELECT started_at, completed_at FROM schedule_executions WHERE id = ?",
        (ex.id,),
    )
    assert row["started_at"].endswith("Z")
    assert row["completed_at"].endswith("Z")


def test_update_execution_status_completed_at_has_z(db):
    ex = db.create_execution("s1", "agent-a", "hello")
    assert db.update_execution_status(ex.id, ExecutionStatus.SUCCESS, response="done")
    row = _raw(db, "SELECT completed_at FROM schedule_executions WHERE id = ?", (ex.id,))
    assert row["completed_at"].endswith("Z")
    assert _ISO_Z_RE.match(row["completed_at"])


def test_update_schedule_run_times_has_z_for_naive_and_aware(db, db_with_data):
    # naive utcnow input → 'Z'
    db.update_schedule_run_times("schedule-1", last_run_at=datetime.utcnow())
    row = _raw(db, "SELECT last_run_at FROM agent_schedules WHERE id = ?", ("schedule-1",))
    assert row["last_run_at"].endswith("Z")

    # aware, non-UTC input → converted to UTC 'Z' (same instant)
    kiev = datetime(2026, 7, 6, 14, 0, tzinfo=timezone(timedelta(hours=3)))
    db.update_schedule_run_times("schedule-1", next_run_at=kiev)
    row = _raw(db, "SELECT next_run_at FROM agent_schedules WHERE id = ?", ("schedule-1",))
    assert row["next_run_at"].endswith("Z")
    # 14:00+03:00 == 11:00Z
    assert row["next_run_at"].startswith("2026-07-06T11:00:00")


def test_schedule_retry_retry_scheduled_at_has_z(db):
    ex = db.create_execution("s1", "agent-a", "hello")
    db.schedule_retry(ex.id, datetime.utcnow() + timedelta(minutes=5))
    row = _raw(
        db, "SELECT retry_scheduled_at FROM schedule_executions WHERE id = ?", (ex.id,)
    )
    assert row["retry_scheduled_at"].endswith("Z")


def test_process_schedule_writes_have_z(db):
    db.ensure_process_schedules_table()
    ps = db.create_process_schedule("p1", "proc", "trig", "0 9 * * *")
    row = _raw(
        db,
        "SELECT created_at, updated_at FROM process_schedules WHERE id = ?",
        (ps.id,),
    )
    assert row["created_at"].endswith("Z")
    assert row["updated_at"].endswith("Z")

    pex = db.create_process_schedule_execution(ps.id, "p1", "proc")
    row = _raw(
        db,
        "SELECT started_at FROM process_schedule_executions WHERE id = ?",
        (pex.id,),
    )
    assert row["started_at"].endswith("Z")

    assert db.update_process_schedule_execution(pex.id, ExecutionStatus.SUCCESS)
    row = _raw(
        db,
        "SELECT completed_at FROM process_schedule_executions WHERE id = ?",
        (pex.id,),
    )
    assert row["completed_at"].endswith("Z")

    db.update_process_schedule_run_times("nope-id", last_run_at=datetime.utcnow())  # no-op OK
    db.update_process_schedule_run_times(ps.id, next_run_at=datetime.utcnow())
    row = _raw(db, "SELECT next_run_at FROM process_schedules WHERE id = ?", (ps.id,))
    assert row["next_run_at"].endswith("Z")


# ---------------------------------------------------------------------------
# Duration math: no aware−naive TypeError after the write change
# ---------------------------------------------------------------------------

def test_update_execution_status_duration_no_typeerror(db):
    ex = db.create_execution("s1", "agent-a", "hello")  # started_at now 'Z'
    assert db.update_execution_status(ex.id, ExecutionStatus.SUCCESS)
    row = _raw(db, "SELECT duration_ms FROM schedule_executions WHERE id = ?", (ex.id,))
    assert row["duration_ms"] is not None
    assert row["duration_ms"] >= 0


def test_update_execution_status_duration_mixed_legacy_naive_started(db):
    """A legacy naive started_at row + a fresh completed → duration computes."""
    started = (datetime.utcnow() - timedelta(seconds=30)).isoformat()  # naive, no 'Z'
    conn = sqlite3.connect(db.database_path)
    conn.execute(
        "INSERT INTO schedule_executions (id, schedule_id, agent_name, status, "
        "started_at, message, triggered_by) VALUES (?,?,?,?,?,?,?)",
        ("legacy1", "s1", "agent-a", ExecutionStatus.RUNNING, started, "m", "schedule"),
    )
    conn.commit()
    conn.close()

    assert db.update_execution_status("legacy1", ExecutionStatus.SUCCESS)
    row = _raw(db, "SELECT duration_ms FROM schedule_executions WHERE id = ?", ("legacy1",))
    assert row["duration_ms"] is not None
    assert 29_000 <= row["duration_ms"] <= 60_000


# ---------------------------------------------------------------------------
# Read side: parse tolerance + instant preservation (naive UTC out)
# ---------------------------------------------------------------------------

def test_read_offset_bearing_row_returns_naive_utc_instant(db):
    """An offset row (+03:00) parses to naive UTC preserving the same instant."""
    conn = sqlite3.connect(db.database_path)
    conn.execute(
        "INSERT INTO schedule_executions (id, schedule_id, agent_name, status, "
        "started_at, message, triggered_by) VALUES (?,?,?,?,?,?,?)",
        ("off1", "s1", "agent-a", ExecutionStatus.RUNNING,
         "2026-07-06T14:00:00.000000+03:00", "m", "schedule"),
    )
    conn.commit()
    conn.close()

    ex = db.get_execution("off1")
    assert ex.started_at.tzinfo is None  # naive model type preserved
    # 14:00+03:00 == 11:00Z
    assert ex.started_at == datetime(2026, 7, 6, 11, 0, 0)


def test_read_legacy_naive_row_ok(db):
    conn = sqlite3.connect(db.database_path)
    conn.execute(
        "INSERT INTO schedule_executions (id, schedule_id, agent_name, status, "
        "started_at, message, triggered_by) VALUES (?,?,?,?,?,?,?)",
        ("naive1", "s1", "agent-a", ExecutionStatus.RUNNING,
         "2026-07-06T11:00:00.000000", "m", "schedule"),
    )
    conn.commit()
    conn.close()

    ex = db.get_execution("naive1")
    assert ex.started_at == datetime(2026, 7, 6, 11, 0, 0)


def test_read_z_row_ok(db):
    ex = db.create_execution("s1", "agent-a", "hello")  # stored with 'Z'
    reread = db.get_execution(ex.id)
    assert reread.started_at.tzinfo is None
    assert isinstance(reread.started_at, datetime)


# ---------------------------------------------------------------------------
# Format parity with the backend
# ---------------------------------------------------------------------------

def test_utc_now_iso_format():
    assert _ISO_Z_RE.match(utc_now_iso())


def test_to_utc_iso_naive_and_aware_match_backend_format():
    # the completed_at path passes a naive utcnow()
    assert _ISO_Z_RE.match(to_utc_iso(datetime.utcnow()))
    # aware inputs are converted, same format
    aware = datetime(2026, 7, 6, 14, 0, 0, 123456, tzinfo=timezone(timedelta(hours=3)))
    out = to_utc_iso(aware)
    assert _ISO_Z_RE.match(out)
    assert out == "2026-07-06T11:00:00.123456Z"


def test_parse_scheduler_ts_roundtrip_is_utc_instant():
    assert parse_scheduler_ts("2026-07-06T11:00:00.000000Z") == datetime(2026, 7, 6, 11, 0, 0)
    assert parse_scheduler_ts("2026-07-06T11:00:00.000000") == datetime(2026, 7, 6, 11, 0, 0)
    assert parse_scheduler_ts("2026-07-06T14:00:00.000000+03:00") == datetime(2026, 7, 6, 11, 0, 0)
