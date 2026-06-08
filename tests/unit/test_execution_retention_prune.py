"""
Unit tests for execution_log + schedule_executions + agent_health_checks
retention pruning (Issue #772, bug fix #862).

Covers:
- ``ScheduleOperations.prune_execution_logs``  — nulls execution_log on
  terminal rows older than the cutoff, preserves recent + non-terminal.
- ``ScheduleOperations.prune_execution_rows``  — deletes terminal rows
  older than the cutoff.
- ``MonitoringOperations.cleanup_old_records`` — chunked DELETE on
  agent_health_checks older than the cutoff.
- ``retention_days <= 0`` disables every sweep.
- ``chunk_size`` is respected (multi-pass drain).

Bug #862 regression coverage (appended at the bottom):
- Pruning uses the correct TaskExecutionStatus values: 'success', 'failed',
  'cancelled', 'skipped'.  The original #772 code used 'completed',
  'terminated' — values that never existed in the enum — so only 'failed'
  rows were ever pruned.  Tests verify all four correct statuses are pruned
  and that old wrong status values are NOT pruned.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_SCHEDULE_EXECUTIONS_DDL = """
CREATE TABLE schedule_executions (
    id TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    duration_ms INTEGER,
    message TEXT NOT NULL,
    response TEXT,
    error TEXT,
    triggered_by TEXT NOT NULL,
    context_used INTEGER,
    context_max INTEGER,
    cost REAL,
    tool_calls TEXT,
    execution_log TEXT,
    model_used TEXT,
    subscription_id TEXT,
    attempt_number INTEGER DEFAULT 1,
    retry_of_execution_id TEXT,
    retry_scheduled_at TEXT,
    business_status TEXT,
    validated_at TEXT,
    validation_execution_id TEXT,
    validates_execution_id TEXT,
    compact_metadata TEXT
)
"""

_AGENT_HEALTH_CHECKS_DDL = """
CREATE TABLE agent_health_checks (
    id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    check_type TEXT NOT NULL,
    status TEXT NOT NULL,
    container_status TEXT,
    cpu_percent REAL,
    memory_percent REAL,
    memory_mb REAL,
    restart_count INTEGER,
    oom_killed INTEGER,
    reachable INTEGER,
    latency_ms REAL,
    runtime_available INTEGER,
    claude_available INTEGER,
    context_percent REAL,
    active_executions INTEGER,
    error_rate REAL,
    error_message TEXT,
    checked_at TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""


@pytest.fixture
def db_setup(tmp_path, monkeypatch):
    """Build a tmp DB with both tables + the #772 partial index.

    Returns (db_path, schedule_ops, monitoring_ops). The connection module
    is reloaded under a unique name so each test gets a fresh path.
    """
    db_path = tmp_path / "trinity.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_path))

    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEDULE_EXECUTIONS_DDL)
    conn.executescript(_AGENT_HEALTH_CHECKS_DDL)
    # Mirror the partial index from schema.py / migration #772 (fixed in #862).
    conn.execute(
        "CREATE INDEX idx_executions_completed_terminal "
        "ON schedule_executions(completed_at) "
        "WHERE status IN ('success', 'failed', 'cancelled', 'skipped')"
    )
    conn.commit()
    conn.close()

    # Reload db/connection.py against the new TRINITY_DB_PATH.
    sys.modules.pop("_erp_db_connection", None)
    _load("_erp_db_connection", _BACKEND / "db" / "connection.py")

    db_pkg = type(sys)("db")
    db_pkg.__path__ = [str(_BACKEND / "db")]
    monkeypatch.setitem(sys.modules, "db", db_pkg)
    monkeypatch.setitem(sys.modules, "db.connection", sys.modules["_erp_db_connection"])

    # Load db.schedules + db.monitoring under the `db.*` namespace so their
    # `from .connection import get_db_connection` resolves to our tmp DB.
    sched_spec = importlib.util.spec_from_file_location(
        "db.schedules", str(_BACKEND / "db" / "schedules.py")
    )
    sched_mod = importlib.util.module_from_spec(sched_spec)
    sys.modules["db.schedules"] = sched_mod
    sched_spec.loader.exec_module(sched_mod)

    mon_spec = importlib.util.spec_from_file_location(
        "db.monitoring", str(_BACKEND / "db" / "monitoring.py")
    )
    mon_mod = importlib.util.module_from_spec(mon_spec)
    sys.modules["db.monitoring"] = mon_mod
    mon_spec.loader.exec_module(mon_mod)

    # ScheduleOperations needs user_ops + agent_ops in __init__, but the
    # prune methods don't touch them. Pass None placeholders.
    schedule_ops = sched_mod.ScheduleOperations(None, None)
    monitoring_ops = mon_mod.MonitoringOperations()
    return db_path, schedule_ops, monitoring_ops


def _iso(days_ago: float) -> str:
    """utc_now_iso()-format timestamp `days_ago` days in the past."""
    return (
        datetime.now(timezone.utc) - timedelta(days=days_ago)
    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _insert_execution(
    db_path: Path,
    *,
    id_: str,
    status: str,
    completed_days_ago: float | None,
    execution_log: str | None,
) -> None:
    started = _iso(completed_days_ago or 0.1)
    completed = _iso(completed_days_ago) if completed_days_ago is not None else None
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO schedule_executions
            (id, schedule_id, agent_name, status,
             started_at, completed_at, message, triggered_by, execution_log)
        VALUES (?, 'sched-x', 'agent-x', ?, ?, ?, 'msg', 'user', ?)
        """,
        (id_, status, started, completed, execution_log),
    )
    conn.commit()
    conn.close()


def _insert_health_check(db_path: Path, *, id_: str, days_ago: float) -> None:
    checked = _iso(days_ago)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO agent_health_checks
            (id, agent_name, check_type, status, checked_at, created_at)
        VALUES (?, 'agent-x', 'aggregate', 'ok', ?, ?)
        """,
        (id_, checked, checked),
    )
    conn.commit()
    conn.close()


def _execution_logs(db_path: Path) -> dict[str, str | None]:
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT id, execution_log FROM schedule_executions").fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}


def _execution_ids(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    ids = {r[0] for r in conn.execute("SELECT id FROM schedule_executions").fetchall()}
    conn.close()
    return ids


def _health_ids(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    ids = {r[0] for r in conn.execute("SELECT id FROM agent_health_checks").fetchall()}
    conn.close()
    return ids


# ---------------------------------------------------------------------------
# prune_execution_logs
# ---------------------------------------------------------------------------


def test_prune_execution_logs_nulls_terminal_past_cutoff(db_setup):
    db_path, schedule_ops, _ = db_setup

    # Past 30-day cutoff, terminal (correct status values): should be nulled.
    _insert_execution(db_path, id_="old-success", status="success",
                      completed_days_ago=45, execution_log="big-jsonl")
    _insert_execution(db_path, id_="old-failed", status="failed",
                      completed_days_ago=60, execution_log="big-jsonl")
    _insert_execution(db_path, id_="old-cancelled", status="cancelled",
                      completed_days_ago=90, execution_log="big-jsonl")
    # Within retention window: keep the log.
    _insert_execution(db_path, id_="recent-success", status="success",
                      completed_days_ago=5, execution_log="keep-me")
    # Non-terminal, must never be touched.
    _insert_execution(db_path, id_="old-running", status="running",
                      completed_days_ago=None, execution_log="active-jsonl")
    # Terminal but log already NULL — should not be counted again.
    _insert_execution(db_path, id_="old-success-nulled", status="success",
                      completed_days_ago=100, execution_log=None)

    nulled = schedule_ops.prune_execution_logs(retention_days=30, chunk_size=1000)
    assert nulled == 3

    logs = _execution_logs(db_path)
    assert logs["old-success"] is None
    assert logs["old-failed"] is None
    assert logs["old-cancelled"] is None
    assert logs["recent-success"] == "keep-me"
    assert logs["old-running"] == "active-jsonl"
    assert logs["old-success-nulled"] is None


def test_prune_execution_logs_respects_chunk_size(db_setup):
    """Multi-pass drain — chunked loop must drain everything in scope."""
    db_path, schedule_ops, _ = db_setup

    for i in range(7):
        _insert_execution(
            db_path, id_=f"old-{i}", status="success",
            completed_days_ago=60, execution_log="payload",
        )

    nulled = schedule_ops.prune_execution_logs(retention_days=30, chunk_size=2)
    assert nulled == 7
    assert all(v is None for v in _execution_logs(db_path).values())


def test_prune_execution_logs_disabled_when_retention_zero(db_setup):
    db_path, schedule_ops, _ = db_setup
    _insert_execution(db_path, id_="old", status="success",
                      completed_days_ago=400, execution_log="payload")

    assert schedule_ops.prune_execution_logs(retention_days=0) == 0
    assert schedule_ops.prune_execution_logs(retention_days=-1) == 0
    assert _execution_logs(db_path)["old"] == "payload"


def test_prune_execution_logs_empty_table(db_setup):
    _, schedule_ops, _ = db_setup
    assert schedule_ops.prune_execution_logs(retention_days=30) == 0


# ---------------------------------------------------------------------------
# prune_execution_rows
# ---------------------------------------------------------------------------


def test_prune_execution_rows_deletes_terminal_past_cutoff(db_setup):
    db_path, schedule_ops, _ = db_setup

    _insert_execution(db_path, id_="old-success", status="success",
                      completed_days_ago=120, execution_log=None)
    _insert_execution(db_path, id_="old-failed", status="failed",
                      completed_days_ago=200, execution_log=None)
    _insert_execution(db_path, id_="recent-success", status="success",
                      completed_days_ago=30, execution_log=None)
    _insert_execution(db_path, id_="old-running", status="running",
                      completed_days_ago=None, execution_log=None)

    deleted = schedule_ops.prune_execution_rows(retention_days=90, chunk_size=1000)
    assert deleted == 2
    assert _execution_ids(db_path) == {"recent-success", "old-running"}


def test_prune_execution_rows_disabled_when_retention_zero(db_setup):
    db_path, schedule_ops, _ = db_setup
    _insert_execution(db_path, id_="old", status="success",
                      completed_days_ago=400, execution_log=None)

    assert schedule_ops.prune_execution_rows(retention_days=0) == 0
    assert _execution_ids(db_path) == {"old"}


# ---------------------------------------------------------------------------
# cleanup_old_records (agent_health_checks)
# ---------------------------------------------------------------------------


def test_cleanup_old_health_records_deletes_past_cutoff(db_setup):
    db_path, _, monitoring_ops = db_setup

    _insert_health_check(db_path, id_="old-1", days_ago=10)
    _insert_health_check(db_path, id_="old-2", days_ago=14)
    _insert_health_check(db_path, id_="recent", days_ago=1)

    deleted = monitoring_ops.cleanup_old_records(days=7, chunk_size=1000)
    assert deleted == 2
    assert _health_ids(db_path) == {"recent"}


def test_cleanup_old_health_records_disabled_when_days_zero(db_setup):
    db_path, _, monitoring_ops = db_setup
    _insert_health_check(db_path, id_="old", days_ago=400)

    assert monitoring_ops.cleanup_old_records(days=0) == 0
    assert _health_ids(db_path) == {"old"}


def test_cleanup_old_health_records_respects_chunk_size(db_setup):
    db_path, _, monitoring_ops = db_setup
    for i in range(5):
        _insert_health_check(db_path, id_=f"old-{i}", days_ago=30)

    deleted = monitoring_ops.cleanup_old_records(days=7, chunk_size=2)
    assert deleted == 5
    assert _health_ids(db_path) == set()


# ---------------------------------------------------------------------------
# Bug #862 regression: correct TaskExecutionStatus values
# ---------------------------------------------------------------------------


def test_prune_logs_all_four_terminal_statuses(db_setup):
    """'success', 'failed', 'cancelled', 'skipped' must all be pruned (#862)."""
    db_path, schedule_ops, _ = db_setup

    for status in ("success", "failed", "cancelled", "skipped"):
        _insert_execution(db_path, id_=f"old-{status}", status=status,
                          completed_days_ago=40, execution_log="payload")

    nulled = schedule_ops.prune_execution_logs(retention_days=30, chunk_size=100)
    assert nulled == 4
    for status in ("success", "failed", "cancelled", "skipped"):
        assert _execution_logs(db_path)[f"old-{status}"] is None


def test_prune_rows_all_four_terminal_statuses(db_setup):
    """'success', 'failed', 'cancelled', 'skipped' rows are all deleted (#862)."""
    db_path, schedule_ops, _ = db_setup

    for status in ("success", "failed", "cancelled", "skipped"):
        _insert_execution(db_path, id_=f"old-{status}", status=status,
                          completed_days_ago=100, execution_log=None)

    deleted = schedule_ops.prune_execution_rows(retention_days=90, chunk_size=100)
    assert deleted == 4
    assert _execution_ids(db_path) == set()


def test_prune_logs_skips_wrong_legacy_status_values(db_setup):
    """'completed' and 'terminated' never existed in TaskExecutionStatus — not pruned (#862)."""
    db_path, schedule_ops, _ = db_setup

    _insert_execution(db_path, id_="legacy-completed", status="completed",
                      completed_days_ago=60, execution_log="payload")
    _insert_execution(db_path, id_="legacy-terminated", status="terminated",
                      completed_days_ago=60, execution_log="payload")

    nulled = schedule_ops.prune_execution_logs(retention_days=30, chunk_size=100)
    assert nulled == 0
    assert _execution_logs(db_path)["legacy-completed"] == "payload"
    assert _execution_logs(db_path)["legacy-terminated"] == "payload"


def test_prune_rows_skips_non_terminal_statuses(db_setup):
    """running, queued, pending_retry rows survive the row-delete sweep (#862)."""
    db_path, schedule_ops, _ = db_setup

    for status in ("running", "queued", "pending_retry"):
        _insert_execution(db_path, id_=f"nterm-{status}", status=status,
                          completed_days_ago=None, execution_log=None)

    deleted = schedule_ops.prune_execution_rows(retention_days=90, chunk_size=100)
    assert deleted == 0
    assert len(_execution_ids(db_path)) == 3
