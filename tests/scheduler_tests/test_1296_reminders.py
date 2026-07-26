"""Agent self-reminders — scheduler-side coverage (#1296).

The standalone scheduler owns arm/fire/reconcile. Covers:
- the single-fire CAS (`claim_reminder_firing`): committed (visible to a fresh
  connection) + a multi-thread/multi-connection contention test where exactly
  ONE claim wins (Codex C8 — a sequential test proves the predicate, not the
  concurrency);
- `_execute_reminder` outcomes: claim-loss → no dispatch; success → firing→fired
  + real execution_id linked; TimeoutException → firing→fired, execution row NOT
  force-FAILED (Codex C2); clean pre-start failure → attempt FAILED + firing→
  pending (retry) until attempts hit MAX → firing→failed (bounded, AC #3);
- `_reconcile_reminders`: arm-once (idempotent), past-due → now+5s, stale-firing
  reclaim, a Z-suffixed fire_at reconciles without raising, table-absent no-op,
  soft-deleted / autonomy-off agents not armed;
- `reload_schedules()` rebuilds/reclaims reminder jobs (Codex C6).
"""

# Path setup must happen before scheduler imports
import sys
from pathlib import Path
_src_path = str(Path(__file__).resolve().parent.parent.parent / "src")
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)

import sqlite3
import threading
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scheduler.database import SchedulerDatabase
from scheduler.models import ExecutionStatus
from scheduler.service import SchedulerService, _reminder_outcome_unknown
from scheduler.config import config
from scheduler.utils import utc_now_iso, to_utc_iso


_REMINDERS_DDL = """
    CREATE TABLE IF NOT EXISTS agent_reminders (
        id TEXT PRIMARY KEY,
        agent_name TEXT NOT NULL,
        message TEXT NOT NULL,
        fire_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        model TEXT,
        timeout_seconds INTEGER,
        allowed_tools TEXT,
        owner_id INTEGER,
        created_by_email TEXT,
        source_agent_name TEXT,
        source_mcp_key_id TEXT,
        execution_id TEXT,
        fire_attempts INTEGER NOT NULL DEFAULT 0,
        firing_at TEXT,
        error TEXT,
        created_at TEXT NOT NULL,
        fired_at TEXT,
        cancelled_at TEXT
    )
"""


def _setup_reminders(db_path: str, *, agent="rem-agent", autonomy=1, deleted_at=None):
    """Create the agent_reminders table + a matching agent_ownership row."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(_REMINDERS_DDL)
    cur.execute(
        "INSERT OR REPLACE INTO agent_ownership (agent_name, owner_id, autonomy_enabled, created_at, deleted_at) "
        "VALUES (?, 1, ?, ?, ?)",
        (agent, autonomy, utc_now_iso(), deleted_at),
    )
    conn.commit()
    conn.close()


def _insert_reminder(db_path, rid, agent="rem-agent", *, fire_at=None, status="pending",
                     fire_attempts=0, firing_at=None, execution_id=None):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO agent_reminders (id, agent_name, message, fire_at, status, "
        "fire_attempts, firing_at, execution_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rid, agent, "do the thing", fire_at or to_utc_iso(datetime.utcnow() + timedelta(hours=1)),
         status, fire_attempts, firing_at, execution_id, utc_now_iso()),
    )
    conn.commit()
    conn.close()


def _read_status(db_path, rid):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM agent_reminders WHERE id = ?", (rid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _service(db, mock_lock_manager):
    svc = SchedulerService(database=db, lock_manager=mock_lock_manager)
    svc.scheduler = MagicMock()
    return svc


# ---------------------------------------------------------------------------
# Classification helper
# ---------------------------------------------------------------------------

def test_outcome_unknown_classification():
    import httpx
    assert _reminder_outcome_unknown(httpx.ReadTimeout("x")) is True
    assert _reminder_outcome_unknown(Exception("dispatch timed out — outcome unknown")) is True
    wrapped = Exception("boom")
    wrapped.__cause__ = httpx.ConnectTimeout("t")
    assert _reminder_outcome_unknown(wrapped) is True
    assert _reminder_outcome_unknown(Exception("Backend execute-task returned 503: warming up")) is False
    assert _reminder_outcome_unknown(httpx.ConnectError("refused")) is False


# ---------------------------------------------------------------------------
# Single-fire CAS
# ---------------------------------------------------------------------------

def test_claim_reminder_firing_cas_sequential(initialized_db):
    _setup_reminders(initialized_db)
    _insert_reminder(initialized_db, "r1")
    db = SchedulerDatabase(database_path=initialized_db)
    assert db.claim_reminder_firing("r1") is True     # first wins
    assert db.claim_reminder_firing("r1") is False    # second loses (no longer pending)
    row = _read_status(initialized_db, "r1")
    assert row["status"] == "firing"
    assert row["fire_attempts"] == 1
    assert row["firing_at"] is not None


def test_claim_reminder_firing_commits(initialized_db):
    """The commit is load-bearing: a FRESH connection must see 'firing'."""
    _setup_reminders(initialized_db)
    _insert_reminder(initialized_db, "r1")
    db = SchedulerDatabase(database_path=initialized_db)
    assert db.claim_reminder_firing("r1") is True
    # A brand-new raw connection (proves the write was committed, not rolled back).
    assert _read_status(initialized_db, "r1")["status"] == "firing"


def test_claim_reminder_firing_multi_connection_contention(initialized_db):
    """Codex C8: N threads, each its OWN SchedulerDatabase (own connection),
    race to claim the SAME reminder — exactly ONE wins."""
    _setup_reminders(initialized_db)
    _insert_reminder(initialized_db, "r1")
    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker():
        db = SchedulerDatabase(database_path=initialized_db)
        barrier.wait()
        for _ in range(20):
            try:
                won = db.claim_reminder_firing("r1")
                break
            except sqlite3.OperationalError:
                # SQLite write-lock contention — retry (prod uses PG or WAL).
                continue
        else:
            won = False
        with lock:
            results.append(won)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for r in results if r) == 1, results


# ---------------------------------------------------------------------------
# _execute_reminder outcomes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_reminder_claim_loss_no_dispatch(initialized_db, mock_lock_manager):
    _setup_reminders(initialized_db)
    _insert_reminder(initialized_db, "r1", status="cancelled")  # not pending → claim loses
    db = SchedulerDatabase(database_path=initialized_db)
    svc = _service(db, mock_lock_manager)
    with patch.object(svc, "_call_backend_execute_task", new_callable=AsyncMock) as backend:
        await svc._execute_reminder("r1", "rem-agent", "m", None, None, None)
    backend.assert_not_called()
    assert _read_status(initialized_db, "r1")["execution_id"] is None


@pytest.mark.asyncio
async def test_execute_reminder_success(initialized_db, mock_lock_manager):
    _setup_reminders(initialized_db)
    _insert_reminder(initialized_db, "r1")
    db = SchedulerDatabase(database_path=initialized_db)
    svc = _service(db, mock_lock_manager)
    with patch.object(svc, "_call_backend_execute_task", new_callable=AsyncMock) as backend:
        backend.return_value = {"status": "dispatched", "async_mode": True}
        await svc._execute_reminder("r1", "rem-agent", "m", "claude-x", 900, ["Bash"])
    # dispatched with triggered_by="reminder" + a REAL execution_id
    kwargs = backend.call_args.kwargs
    assert kwargs["triggered_by"] == "reminder"
    assert kwargs["execution_id"] is not None
    row = _read_status(initialized_db, "r1")
    assert row["status"] == "fired"
    assert row["execution_id"] == kwargs["execution_id"]
    # execution row exists + RUNNING (the poll owns its terminal)
    ex = db.get_execution(row["execution_id"])
    assert ex is not None and ex.triggered_by == "reminder"


@pytest.mark.asyncio
async def test_execute_reminder_timeout_assume_dispatched(initialized_db, mock_lock_manager):
    _setup_reminders(initialized_db)
    _insert_reminder(initialized_db, "r1")
    db = SchedulerDatabase(database_path=initialized_db)
    svc = _service(db, mock_lock_manager)
    with patch.object(svc, "_call_backend_execute_task", new_callable=AsyncMock) as backend:
        backend.side_effect = Exception(
            "dispatch to /api/internal/execute-task timed out after 30s — outcome unknown"
        )
        await svc._execute_reminder("r1", "rem-agent", "m", None, None, None)
    row = _read_status(initialized_db, "r1")
    assert row["status"] == "fired"  # assume-dispatched
    # execution row NOT force-FAILED (still RUNNING — the poll finalizes it)
    ex = db.get_execution(row["execution_id"])
    assert ex.status == ExecutionStatus.RUNNING


@pytest.mark.asyncio
async def test_execute_reminder_clean_failure_retries(initialized_db, mock_lock_manager):
    _setup_reminders(initialized_db)
    _insert_reminder(initialized_db, "r1")
    db = SchedulerDatabase(database_path=initialized_db)
    svc = _service(db, mock_lock_manager)
    with patch.object(svc, "_call_backend_execute_task", new_callable=AsyncMock) as backend:
        backend.side_effect = Exception("Backend execute-task returned 503: warming up")
        await svc._execute_reminder("r1", "rem-agent", "m", None, None, None)
    row = _read_status(initialized_db, "r1")
    assert row["status"] == "pending"      # released for retry (attempt 1 < MAX)
    assert row["fire_attempts"] == 1
    # the attempt's execution row was marked FAILED (status-guarded)
    ex = db.get_execution(row["execution_id"])
    assert ex.status == ExecutionStatus.FAILED


@pytest.mark.asyncio
async def test_execute_reminder_bounded_failed(initialized_db, mock_lock_manager):
    _setup_reminders(initialized_db)
    # Start one attempt below the cap; this attempt takes it to MAX → failed.
    _insert_reminder(initialized_db, "r1", fire_attempts=config.max_reminder_fire_attempts - 1)
    db = SchedulerDatabase(database_path=initialized_db)
    svc = _service(db, mock_lock_manager)
    with patch.object(svc, "_call_backend_execute_task", new_callable=AsyncMock) as backend:
        backend.side_effect = Exception("Backend execute-task returned 503: warming up")
        await svc._execute_reminder("r1", "rem-agent", "m", None, None, None)
    row = _read_status(initialized_db, "r1")
    assert row["status"] == "failed"       # bounded terminal
    assert row["fire_attempts"] == config.max_reminder_fire_attempts


# ---------------------------------------------------------------------------
# _reconcile_reminders
# ---------------------------------------------------------------------------

def test_reconcile_arms_pending_once(initialized_db, mock_lock_manager):
    _setup_reminders(initialized_db)
    _insert_reminder(initialized_db, "r1")
    db = SchedulerDatabase(database_path=initialized_db)
    svc = _service(db, mock_lock_manager)
    svc.scheduler.get_job.return_value = None
    svc._reconcile_reminders()
    assert svc.scheduler.add_job.call_count == 1
    assert svc.scheduler.add_job.call_args.kwargs["id"] == "reminder_r1"
    # Now a live job exists → not re-armed.
    svc.scheduler.get_job.return_value = MagicMock()
    svc.scheduler.add_job.reset_mock()
    svc._reconcile_reminders()
    svc.scheduler.add_job.assert_not_called()


def test_reconcile_past_due_arms_soon(initialized_db, mock_lock_manager):
    _setup_reminders(initialized_db)
    _insert_reminder(initialized_db, "r1",
                     fire_at=to_utc_iso(datetime.utcnow() - timedelta(hours=2)))
    db = SchedulerDatabase(database_path=initialized_db)
    svc = _service(db, mock_lock_manager)
    svc.scheduler.get_job.return_value = None
    svc._reconcile_reminders()
    run_at = svc.scheduler.add_job.call_args.kwargs["trigger"].run_date
    # armed for ~now+5s (past-due), not the far-past fire_at
    now = datetime.utcnow()
    delta = (run_at.replace(tzinfo=None) - now).total_seconds()
    assert -2 < delta < 30


def test_reconcile_stale_firing_reclaimed(initialized_db, mock_lock_manager):
    _setup_reminders(initialized_db)
    stale = to_utc_iso(datetime.utcnow() - timedelta(hours=1))
    _insert_reminder(initialized_db, "r1", status="firing", firing_at=stale, fire_attempts=1)
    db = SchedulerDatabase(database_path=initialized_db)
    svc = _service(db, mock_lock_manager)
    svc.scheduler.get_job.return_value = None  # no live job → crash-mid-fire orphan
    svc._reconcile_reminders()
    # attempt 1 < MAX → released to pending for the next tick to re-arm
    assert _read_status(initialized_db, "r1")["status"] == "pending"


def test_reconcile_z_suffix_no_raise(initialized_db, mock_lock_manager):
    _setup_reminders(initialized_db)
    # Explicit Z-suffixed absolute time (the #1472/#1474 offset-vs-naive trap).
    _insert_reminder(initialized_db, "r1", fire_at="2030-01-01T00:00:00.000000Z")
    db = SchedulerDatabase(database_path=initialized_db)
    svc = _service(db, mock_lock_manager)
    svc.scheduler.get_job.return_value = None
    svc._reconcile_reminders()  # must not raise
    assert svc.scheduler.add_job.call_count == 1


def test_reconcile_missing_table_noop(initialized_db, mock_lock_manager):
    # No agent_reminders table created → clean no-op (new scheduler image before
    # the backend applies 0028).
    db = SchedulerDatabase(database_path=initialized_db)
    svc = _service(db, mock_lock_manager)
    svc._reconcile_reminders()  # must not raise
    svc.scheduler.add_job.assert_not_called()


def test_reconcile_soft_deleted_agent_not_armed(initialized_db, mock_lock_manager):
    _setup_reminders(initialized_db, deleted_at=utc_now_iso())
    _insert_reminder(initialized_db, "r1")
    db = SchedulerDatabase(database_path=initialized_db)
    svc = _service(db, mock_lock_manager)
    svc.scheduler.get_job.return_value = None
    svc._reconcile_reminders()
    svc.scheduler.add_job.assert_not_called()


def test_reconcile_autonomy_off_not_armed(initialized_db, mock_lock_manager):
    _setup_reminders(initialized_db, autonomy=0)
    _insert_reminder(initialized_db, "r1")
    db = SchedulerDatabase(database_path=initialized_db)
    svc = _service(db, mock_lock_manager)
    svc.scheduler.get_job.return_value = None
    svc._reconcile_reminders()
    svc.scheduler.add_job.assert_not_called()


def test_reload_schedules_reclaims_reminders(initialized_db, mock_lock_manager):
    _setup_reminders(initialized_db)
    _insert_reminder(initialized_db, "r1")
    db = SchedulerDatabase(database_path=initialized_db)
    db.ensure_process_schedules_table()  # normally created by initialize()
    svc = _service(db, mock_lock_manager)
    svc.scheduler.get_jobs.return_value = []
    svc.scheduler.get_job.return_value = None
    svc.reload_schedules()
    # the full-reload path also armed the reminder job (Codex C6)
    ids = [c.kwargs.get("id") for c in svc.scheduler.add_job.call_args_list]
    assert "reminder_r1" in ids
