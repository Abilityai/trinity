"""Tests for per-schedule execution analytics (#868).

Exercises the `ScheduleOperations.get_schedule_analytics` DB function
against an ephemeral SQLite. Same fixture pattern as
`tests/unit/test_schedule_soft_delete.py`:

  * `monkeypatch.setattr` on the module attribute of
    `db.connection.DB_PATH` routes the production helper at our
    tmp file.
  * An autouse fixture pops any stale sibling stubs from
    `sys.modules` so this file's imports re-resolve fresh.

Locked behaviour (from the /autoplan review on the issue):
  * Percentiles computed in Python via ``statistics.quantiles``
    over the newest 5,000 success rows (sampling cap).
  * Counts and the daily timeline use the full unsampled rowset.
  * Tool-call top-5 weighted by ``sum(duration_ms)`` per tool
    (not raw count) — defends against `Read`/`Bash` dominating.
  * Tenant boundary: a caller with access to agent A passing
    agent B's ``schedule_id`` resolves to ``None`` (→ 404 in the
    router), not a successful read.
  * Soft-deleted schedules resolve to ``None`` (matches
    ``get_schedule`` semantics from #834).
  * Timeline is gap-filled in Python so days with zero
    executions still render in the response.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


# ----------------------------------------------------------------------
# Make `src/backend/` importable without depending on conftest hooks
# that test_schedule_soft_delete.py also relies on.
# ----------------------------------------------------------------------
_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


# Mirror the pollution defence from `test_schedule_soft_delete.py` —
# sibling tests stub `db.*` modules pointing at their own tmp DBs and
# never restore on teardown.
_STUBBED_MODULE_NAMES = [
    "db.schedules",
    "db.users",
    "db.agents",
    "db.monitoring",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {n: sys.modules.get(n) for n in _STUBBED_MODULE_NAMES}
    for name in _STUBBED_MODULE_NAMES:
        sys.modules.pop(name, None)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


# ----------------------------------------------------------------------
# Schema helpers — only what get_schedule_analytics actually reads.
# ----------------------------------------------------------------------

def _make_db_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT,
            role TEXT DEFAULT 'user',
            auth0_sub TEXT,
            name TEXT,
            picture TEXT,
            email TEXT,
            created_at TEXT,
            updated_at TEXT,
            last_login TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE agent_schedules (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            name TEXT NOT NULL,
            cron_expression TEXT NOT NULL,
            message TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            timezone TEXT DEFAULT 'UTC',
            description TEXT,
            owner_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_run_at TEXT,
            next_run_at TEXT,
            timeout_seconds INTEGER DEFAULT 900,
            allowed_tools TEXT,
            model TEXT,
            max_retries INTEGER DEFAULT 0,
            retry_delay_seconds INTEGER DEFAULT 60,
            validation_enabled INTEGER DEFAULT 0,
            validation_prompt TEXT,
            validation_timeout_seconds INTEGER DEFAULT 120,
            webhook_token TEXT,
            webhook_enabled INTEGER DEFAULT 0,
            deleted_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE schedule_executions (
            id TEXT PRIMARY KEY,
            schedule_id TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            duration_ms INTEGER,
            cost REAL,
            tool_calls TEXT,
            triggered_by TEXT NOT NULL DEFAULT 'manual',
            message TEXT NOT NULL DEFAULT ''
        )
        """
    )
    # The real schema indexes (schedule_id) and (agent_name, started_at).
    cur.execute(
        "CREATE INDEX idx_executions_schedule ON schedule_executions(schedule_id)"
    )
    cur.execute(
        "INSERT INTO users(id, username, role) VALUES (1, 'owner', 'user')"
    )
    conn.commit()


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "trinity.db"
    conn = sqlite3.connect(str(db_path))
    _make_db_schema(conn)
    conn.close()

    monkeypatch.setenv("TRINITY_DB_PATH", str(db_path))
    monkeypatch.delitem(sys.modules, "db.connection", raising=False)
    try:
        import db.connection as connection_mod
    except ImportError:
        pytest.skip("backend venv required")
    monkeypatch.setattr(connection_mod, "DB_PATH", str(db_path))
    return str(db_path)


@pytest.fixture
def ops(tmp_db):
    try:
        from db.schedules import ScheduleOperations
        from db.users import UserOperations
        from db.agents import AgentOperations
    except ImportError:
        pytest.skip("backend venv required")
    user_ops = UserOperations()
    agent_ops = AgentOperations(user_ops)
    return ScheduleOperations(user_ops, agent_ops)


# ----------------------------------------------------------------------
# Seeding helpers.
# ----------------------------------------------------------------------

def _seed_schedule(
    db_path: str,
    sid: str,
    agent_name: str = "agent-1",
    deleted_at: str | None = None,
) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO agent_schedules
            (id, agent_name, name, cron_expression, message,
             enabled, owner_id, created_at, updated_at, deleted_at)
        VALUES (?, ?, 'sched', '0 0 * * *', 'hi',
                1, 1, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', ?)
        """,
        (sid, agent_name, deleted_at),
    )
    conn.commit()
    conn.close()


def _iso_ago(minutes: int = 0, hours: int = 0, days: int = 0) -> str:
    """ISO-Z timestamp `minutes`/`hours`/`days` in the past — matches
    the format `utc_now_iso()` writes (which the analytics SQL filters
    against lexicographically via `iso_cutoff`)."""
    when = datetime.now(timezone.utc) - timedelta(
        minutes=minutes, hours=hours, days=days,
    )
    return when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _seed_execution(
    db_path: str,
    sid: str,
    agent_name: str = "agent-1",
    *,
    started_at: str | None = None,
    status: str = "success",
    duration_ms: int | None = 1000,
    cost: float | None = 0.01,
    tool_calls: list | str | None = None,
    exec_id: str | None = None,
) -> str:
    if started_at is None:
        started_at = _iso_ago(minutes=5)
    if exec_id is None:
        exec_id = f"e-{sid}-{started_at[-10:]}-{status}-{duration_ms}"
    if isinstance(tool_calls, list):
        tool_calls = json.dumps(tool_calls)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO schedule_executions
            (id, schedule_id, agent_name, status, started_at,
             duration_ms, cost, tool_calls, triggered_by, message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'schedule', '')
        """,
        (exec_id, sid, agent_name, status, started_at,
         duration_ms, cost, tool_calls),
    )
    conn.commit()
    conn.close()
    return exec_id


# ----------------------------------------------------------------------
# Test cases (mirror the issue body's acceptance criteria).
# ----------------------------------------------------------------------

class TestPercentileCorrectness:
    def test_known_dataset_percentiles(self, tmp_db, ops):
        """durations [100,200,...,1000] → known p50/p95/p99 via
        statistics.quantiles(n=100, method='inclusive')."""
        _seed_schedule(tmp_db, "sid-1")
        for d in range(100, 1001, 100):  # 10 values
            _seed_execution(tmp_db, "sid-1", duration_ms=d, status="success")

        out = ops.get_schedule_analytics("sid-1", 24, agent_name="agent-1")
        assert out is not None
        assert out["total_executions"] == 10
        assert out["success_count"] == 10
        p = out["duration_ms"]
        # `inclusive` on [100..1000 by 100] places p50 ≈ 550, p95 ≈ 955,
        # p99 ≈ 991. Allow ±50ms tolerance — the exact stdlib value
        # depends on interpolation, not on us.
        assert 450 <= p["p50"] <= 650
        assert 850 <= p["p95"] <= 1000
        assert 900 <= p["p99"] <= 1000


class TestWindowBoundary:
    def test_execution_outside_window_excluded(self, tmp_db, ops):
        _seed_schedule(tmp_db, "sid-1")
        # Inside 24h window.
        _seed_execution(
            tmp_db, "sid-1",
            started_at=_iso_ago(hours=1),
            duration_ms=100,
        )
        # Outside 24h window.
        _seed_execution(
            tmp_db, "sid-1",
            started_at=_iso_ago(hours=48),
            duration_ms=999,
            exec_id="exec-outside",
        )
        out = ops.get_schedule_analytics("sid-1", 24, agent_name="agent-1")
        assert out["total_executions"] == 1


class TestEmptySchedule:
    def test_no_executions(self, tmp_db, ops):
        _seed_schedule(tmp_db, "sid-1")
        out = ops.get_schedule_analytics("sid-1", 24, agent_name="agent-1")
        assert out is not None
        assert out["total_executions"] == 0
        assert out["success_count"] == 0
        assert out["success_rate"] == 0.0
        assert out["duration_ms"] == {"p50": None, "p95": None, "p99": None}
        assert out["cost"]["total"] == 0
        assert out["tool_calls"] == {"top": [], "total_calls": 0}
        # Even with no executions, timeline is gap-filled so the
        # frontend has bars to render.
        assert len(out["timeline"]) >= 1


class TestAllRunningSchedule:
    def test_only_running_rows(self, tmp_db, ops):
        """Percentiles must be None when there are zero successful rows,
        even if non-terminal executions exist. Counts still populate."""
        _seed_schedule(tmp_db, "sid-1")
        for i in range(3):
            _seed_execution(
                tmp_db, "sid-1",
                exec_id=f"r-{i}",
                status="running",
                duration_ms=None,
            )
        out = ops.get_schedule_analytics("sid-1", 24, agent_name="agent-1")
        assert out["total_executions"] == 3
        assert out["success_count"] == 0
        assert out["duration_ms"]["p50"] is None
        assert out["duration_ms"]["p95"] is None
        assert out["duration_ms"]["p99"] is None


class TestNullDurations:
    def test_null_durations_excluded_from_percentile_pool(self, tmp_db, ops):
        """`duration_ms IS NULL` rows count toward totals but are
        excluded from the percentile subquery."""
        _seed_schedule(tmp_db, "sid-1")
        _seed_execution(
            tmp_db, "sid-1",
            exec_id="d-null", status="success", duration_ms=None,
        )
        _seed_execution(
            tmp_db, "sid-1",
            exec_id="d-100", status="success", duration_ms=100,
        )
        _seed_execution(
            tmp_db, "sid-1",
            exec_id="d-200", status="success", duration_ms=200,
        )
        out = ops.get_schedule_analytics("sid-1", 24, agent_name="agent-1")
        # Total counts include the NULL-duration row.
        assert out["total_executions"] == 3
        assert out["success_count"] == 3
        # Percentiles only saw the two non-null durations.
        assert out["duration_ms"]["p50"] is not None
        assert 100 <= out["duration_ms"]["p50"] <= 200


class TestMalformedToolCalls:
    def test_malformed_json_skipped(self, tmp_db, ops, caplog):
        """An old execution with non-JSON `tool_calls` must not 500
        the analytics call — the loop catches and WARN-logs."""
        _seed_schedule(tmp_db, "sid-1")
        _seed_execution(
            tmp_db, "sid-1",
            exec_id="ok",
            tool_calls=[{"name": "Read", "duration_ms": 50}],
        )
        _seed_execution(
            tmp_db, "sid-1",
            exec_id="bad",
            tool_calls="{not valid json",  # bytes-on-disk garbage
        )
        out = ops.get_schedule_analytics("sid-1", 24, agent_name="agent-1")
        # Should NOT raise.
        assert out["total_executions"] == 2
        # The well-formed entry still surfaces in the top.
        assert any(t["name"] == "Read" for t in out["tool_calls"]["top"])


class TestCrossTenant:
    def test_schedule_id_belonging_to_other_agent_returns_none(self, tmp_db, ops):
        """Caller has access to `agent-A`. Schedule `sid-B` belongs to
        `agent-B`. `get_schedule_analytics(sid-B, agent_name='agent-A')`
        must return None — the router maps to 404, closing the leak."""
        _seed_schedule(tmp_db, "sid-A", agent_name="agent-A")
        _seed_schedule(tmp_db, "sid-B", agent_name="agent-B")
        _seed_execution(tmp_db, "sid-B", agent_name="agent-B")
        out = ops.get_schedule_analytics(
            "sid-B", 24, agent_name="agent-A",
        )
        assert out is None

class TestSoftDeleted:
    def test_soft_deleted_schedule_returns_none(self, tmp_db, ops):
        _seed_schedule(
            tmp_db, "sid-1",
            deleted_at="2026-01-01T00:00:00Z",
        )
        _seed_execution(tmp_db, "sid-1")
        out = ops.get_schedule_analytics("sid-1", 24, agent_name="agent-1")
        assert out is None


class TestSampling:
    """Monkeypatch the module constant rather than threading a test-only
    kwarg through the production signature."""

    def test_sampled_when_over_cap(self, tmp_db, ops, monkeypatch):
        import db.schedules as schedules_mod
        monkeypatch.setattr(schedules_mod, "_PERCENTILE_ROWSET_CAP", 5)

        _seed_schedule(tmp_db, "sid-1")
        for i in range(12):
            _seed_execution(
                tmp_db, "sid-1",
                exec_id=f"e-{i}",
                started_at=_iso_ago(minutes=i),  # newest first when i=0
                duration_ms=(i + 1) * 100,
                tool_calls=[{"name": "Read", "duration_ms": 10}],
            )
        out = ops.get_schedule_analytics("sid-1", 24, agent_name="agent-1")
        assert out["total_executions"] == 12  # unsampled counts
        assert out["sampled"] is True
        assert out["sample_size"] == 5

    def test_not_sampled_when_under_cap(self, tmp_db, ops, monkeypatch):
        import db.schedules as schedules_mod
        monkeypatch.setattr(schedules_mod, "_PERCENTILE_ROWSET_CAP", 5)

        _seed_schedule(tmp_db, "sid-1")
        for i in range(3):
            _seed_execution(tmp_db, "sid-1", exec_id=f"e-{i}", duration_ms=100)
        out = ops.get_schedule_analytics("sid-1", 24, agent_name="agent-1")
        assert out["sampled"] is False
        assert out["sample_size"] == 3


class TestTimelineGapFill:
    def test_zero_day_present_in_timeline(self, tmp_db, ops):
        """A schedule with one execution today and none yesterday must
        still surface a yesterday-bucket with zeros — the frontend
        bar chart depends on a continuous x-axis."""
        _seed_schedule(tmp_db, "sid-1")
        _seed_execution(
            tmp_db, "sid-1",
            started_at=_iso_ago(hours=1),
            duration_ms=100,
        )
        # Request a 7-day window so we get ≥7 buckets.
        out = ops.get_schedule_analytics("sid-1", 168, agent_name="agent-1")
        # The window covers ~8 unique UTC days (start day + 7 full days).
        assert len(out["timeline"]) >= 7
        # Find a zero-day (any day other than today is zero).
        zero_days = [b for b in out["timeline"] if b["success"] == 0 and b["failed"] == 0]
        assert len(zero_days) >= 1
        # Each bucket has the locked-shape keys.
        for b in out["timeline"]:
            assert set(b.keys()) == {"date", "success", "failed", "cost"}


class TestToolCallWeighting:
    def test_top5_weighted_by_duration_not_count(self, tmp_db, ops):
        """`Read` called 10 times for 1ms each (total 10ms) should
        lose to `Bash` called once for 5000ms — top-5 is by total
        wall time, not count. Defends against Strategy finding #6."""
        _seed_schedule(tmp_db, "sid-1")
        # 10 cheap Read calls.
        for i in range(10):
            _seed_execution(
                tmp_db, "sid-1",
                exec_id=f"r-{i}", duration_ms=100,
                tool_calls=[{"name": "Read", "duration_ms": 1}],
            )
        # 1 expensive Bash call.
        _seed_execution(
            tmp_db, "sid-1",
            exec_id="bash-1", duration_ms=100,
            tool_calls=[{"name": "Bash", "duration_ms": 5000}],
        )
        out = ops.get_schedule_analytics("sid-1", 24, agent_name="agent-1")
        top = out["tool_calls"]["top"]
        assert top[0]["name"] == "Bash"
        assert top[0]["total_duration_ms"] == 5000
