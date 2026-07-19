"""Tests for agent-scoped execution analytics (#1107).

Exercises `ScheduleOperations.get_agent_analytics` against an ephemeral
SQLite. Same fixture machinery as `tests/unit/test_schedule_analytics.py`
(#868) — `db.connection.DB_PATH` is monkeypatched at a tmp file and stale
sibling `db.*` stubs are popped from `sys.modules`.

Locked behaviour (from the /autoplan review on the issue):
  * `triggered_by` is grouped into user-facing buckets (Chat/Tasks, MCP,
    Channels, Public, Scheduled, Loops, Agent-to-agent, Voice) with an
    "Other" catch-all so unmapped triggers stay visible.
  * Headline duration `avg` + `context_avg` come from the FULL rowset
    (SQL AVG) — never the capped percentile pool. Only `p95` is sampled.
  * `success_rate` is terminal-based: success / (success + failed),
    where failed = status in ('failed','error'). Days with zero terminal
    rows report `success_rate=None` (chart renders a gap, not a 0%).
  * `context_avg` uses NULL-skipping AVG (unmeasured rows don't read as 0).
  * Timeline is gap-filled in Python so zero-days still render.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


_STUBBED_MODULE_NAMES = [
    "db.schedules",
    "db.users",
    "db.agents",
    "db.monitoring",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    top_names = _STUBBED_MODULE_NAMES + ["db.connection"]
    # #1481: db.schedules is now a PACKAGE — evict/restore its loaded children
    # too, so a pop("db.schedules") can't leave a stale db.schedules.<slice>
    # shadowing the fresh import (and so the analytics _PERCENTILE_ROWSET_CAP
    # monkeypatch binds the freshly-imported db.schedules.analytics module).
    child_names = [m for m in sys.modules if m.startswith("db.schedules.")]
    saved = {n: sys.modules.get(n) for n in top_names + child_names}
    # Re-imports inside the fixtures also rebind the parent `db` package
    # attributes (`import db.X` sets `db.X` on the package). Later test files
    # that bind via `import db.X as Y` resolve through the package attribute,
    # not sys.modules — if only sys.modules is restored, they get a different
    # module object than `from db.X import ...` and attribute patches land on
    # the wrong module (the test_agent_soft_delete DB_PATH mismatch).
    db_pkg = sys.modules.get("db")
    saved_attrs = (
        {n.split(".", 1)[1]: getattr(db_pkg, n.split(".", 1)[1], None) for n in top_names}
        if db_pkg is not None
        else {}
    )
    for name in _STUBBED_MODULE_NAMES + child_names:
        sys.modules.pop(name, None)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
        for attr, value in saved_attrs.items():
            if value is None:
                if hasattr(db_pkg, attr):
                    delattr(db_pkg, attr)
            else:
                setattr(db_pkg, attr, value)


def _make_db_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT,
            role TEXT DEFAULT 'user',
            auth0_sub TEXT, name TEXT, picture TEXT, email TEXT,
            created_at TEXT, updated_at TEXT, last_login TEXT
        )
        """
    )
    # Only the columns get_agent_analytics reads. context_used added (#1107).
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
            context_used INTEGER,
            tool_calls TEXT,
            triggered_by TEXT NOT NULL DEFAULT 'manual',
            message TEXT NOT NULL DEFAULT ''
        )
        """
    )
    cur.execute(
        "CREATE INDEX idx_executions_agent_started "
        "ON schedule_executions(agent_name, started_at DESC)"
    )
    cur.execute("INSERT INTO users(id, username, role) VALUES (1, 'owner', 'user')")
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


def _iso_ago(minutes: int = 0, hours: int = 0, days: int = 0) -> str:
    when = datetime.now(timezone.utc) - timedelta(
        minutes=minutes, hours=hours, days=days,
    )
    return when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


_seq = [0]


def _seed(
    db_path: str,
    agent_name: str = "agent-1",
    *,
    started_at: str | None = None,
    status: str = "success",
    duration_ms: int | None = 1000,
    context_used: int | None = None,
    triggered_by: str = "manual",
    exec_id: str | None = None,
) -> str:
    if started_at is None:
        started_at = _iso_ago(minutes=5)
    if exec_id is None:
        _seq[0] += 1
        exec_id = f"e-{_seq[0]}"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO schedule_executions
            (id, schedule_id, agent_name, status, started_at,
             duration_ms, context_used, triggered_by, message)
        VALUES (?, 'sid', ?, ?, ?, ?, ?, ?, '')
        """,
        (exec_id, agent_name, status, started_at,
         duration_ms, context_used, triggered_by),
    )
    conn.commit()
    conn.close()
    return exec_id


# ----------------------------------------------------------------------


class TestBucketing:
    def test_raw_triggers_grouped_into_buckets(self, tmp_db, ops):
        seeds = {
            "chat": 3, "manual": 2,        # -> Chat/Tasks (5)
            "mcp": 1,                       # -> MCP (1)
            "telegram": 2, "whatsapp": 1,   # -> Channels (3)
            "public": 1, "paid": 1,         # -> Public (2)
            "schedule": 4, "webhook": 1,    # -> Scheduled (5)
            "loop": 2,                       # -> Loops (2), #1150
            "agent": 2, "fan_out": 1,       # -> Agent-to-agent (3)
            "voip": 1,                       # -> Voice (1)
            "validation": 2,                 # -> Other (2)
        }
        for trig, n in seeds.items():
            for _ in range(n):
                _seed(tmp_db, triggered_by=trig)

        out = ops.get_agent_analytics("agent-1", 24)
        totals = {b["bucket"]: b["total"] for b in out["by_type"]}
        assert totals == {
            "Chat/Tasks": 5, "MCP": 1, "Channels": 3, "Public": 2,
            "Scheduled": 5, "Loops": 2, "Agent-to-agent": 3, "Voice": 1,
            "Other": 2,
        }
        # buckets are emitted in canonical stack order
        assert out["buckets"] == [
            "Chat/Tasks", "MCP", "Channels", "Public",
            "Scheduled", "Loops", "Agent-to-agent", "Voice", "Other",
        ]
        assert out["total_executions"] == sum(seeds.values())

    def test_unmapped_trigger_falls_into_other(self, tmp_db, ops):
        _seed(tmp_db, triggered_by="some_future_channel")
        out = ops.get_agent_analytics("agent-1", 24)
        assert {b["bucket"] for b in out["by_type"]} == {"Other"}

    def test_loop_is_its_own_bucket_not_scheduled_or_other(self, tmp_db, ops):
        """#1150: loop runs must not fold into Scheduled (the pre-#1150
        mapping) nor leak into the Other catch-all."""
        _seed(tmp_db, triggered_by="loop")
        out = ops.get_agent_analytics("agent-1", 24)
        assert out["by_type"] == [{"bucket": "Loops", "total": 1}]


class TestSuccessRateTerminal:
    def test_terminal_based_rate(self, tmp_db, ops):
        for _ in range(7):
            _seed(tmp_db, status="success")
        for _ in range(2):
            _seed(tmp_db, status="failed")
        _seed(tmp_db, status="error")          # error counts as failed
        _seed(tmp_db, status="running", duration_ms=None)  # excluded from rate
        out = ops.get_agent_analytics("agent-1", 24)
        assert out["success_count"] == 7
        assert out["failed_count"] == 3        # 2 failed + 1 error
        # 7 / (7 + 3) = 0.7 — running row does not dilute it
        assert out["success_rate"] == 0.7
        assert out["total_executions"] == 11


class TestDurationAvgFullSetVsSampledP95:
    """The correctness fix locked by /autoplan engineering review: `avg`
    must be the FULL-set mean, never the mean of the capped p95 pool."""

    def test_avg_is_full_set_p95_is_sampled(self, tmp_db, ops, monkeypatch):
        import db.schedules.analytics as schedules_mod  # #1481: constant now defined here
        monkeypatch.setattr(schedules_mod, "_PERCENTILE_ROWSET_CAP", 5)

        # 5 newest rows = 100ms, 5 oldest = 1000ms. Full-set avg = 550.
        # The capped pool (newest 5) is all-100 → a sampled avg would be 100.
        for i in range(5):
            _seed(tmp_db, started_at=_iso_ago(minutes=i), duration_ms=100)
        for i in range(10, 15):
            _seed(tmp_db, started_at=_iso_ago(minutes=i), duration_ms=1000)

        out = ops.get_agent_analytics("agent-1", 24)
        # avg over ALL 10 rows — proves it isn't from the capped pool
        assert out["duration_ms"]["avg"] == 550
        # p95 over the newest-5 pool (all 100ms)
        assert out["duration_ms"]["p95"] == 100
        assert out["sampled"] is True
        assert out["sample_size"] == 5

    def test_avg_excludes_failed_durations(self, tmp_db, ops):
        _seed(tmp_db, status="success", duration_ms=200)
        _seed(tmp_db, status="failed", duration_ms=9999)  # not in success avg
        out = ops.get_agent_analytics("agent-1", 24)
        assert out["duration_ms"]["avg"] == 200


class TestContextAvgNullSkipping:
    def test_null_context_skipped_not_zeroed(self, tmp_db, ops):
        _seed(tmp_db, context_used=1000)
        _seed(tmp_db, context_used=3000)
        _seed(tmp_db, context_used=None)   # unmeasured — must NOT read as 0
        out = ops.get_agent_analytics("agent-1", 24)
        # (1000 + 3000) / 2 = 2000, not /3
        assert out["context_avg"] == 2000

    def test_no_context_rows_yields_none(self, tmp_db, ops):
        _seed(tmp_db, context_used=None)
        out = ops.get_agent_analytics("agent-1", 24)
        assert out["context_avg"] is None


class TestWindowBoundary:
    def test_outside_window_excluded(self, tmp_db, ops):
        _seed(tmp_db, started_at=_iso_ago(hours=1))
        _seed(tmp_db, started_at=_iso_ago(hours=48))
        out = ops.get_agent_analytics("agent-1", 24)
        assert out["total_executions"] == 1

    def test_other_agent_excluded(self, tmp_db, ops):
        _seed(tmp_db, agent_name="agent-1")
        _seed(tmp_db, agent_name="agent-2")
        out = ops.get_agent_analytics("agent-1", 24)
        assert out["total_executions"] == 1


class TestEmptyAgent:
    def test_zero_executions_envelope(self, tmp_db, ops):
        out = ops.get_agent_analytics("agent-1", 24)
        assert out["total_executions"] == 0
        assert out["success_rate"] == 0.0
        assert out["duration_ms"] == {"avg": None, "p95": None}
        assert out["context_avg"] is None
        assert out["by_type"] == []
        assert out["buckets"] == []
        assert out["sampled"] is False
        # timeline still gap-filled so the chart has an axis
        assert len(out["timeline"]) >= 1


class TestTimelineGapFill:
    def test_zero_days_present_and_rate_null(self, tmp_db, ops):
        _seed(tmp_db, started_at=_iso_ago(hours=1))
        out = ops.get_agent_analytics("agent-1", 168)  # 7d window
        assert len(out["timeline"]) >= 7
        # Days with no executions report success_rate=None (chart gap),
        # not a false 0.0.
        zero_days = [p for p in out["timeline"] if p["total"] == 0]
        assert len(zero_days) >= 1
        assert all(p["success_rate"] is None for p in zero_days)
        # Locked per-point shape.
        for p in out["timeline"]:
            assert set(p.keys()) == {
                "date", "total", "success", "failed",
                "success_rate", "duration_avg_ms", "context_avg", "by_type",
            }

    def test_day_stacks_present_in_by_type(self, tmp_db, ops):
        _seed(tmp_db, started_at=_iso_ago(hours=1), triggered_by="chat")
        _seed(tmp_db, started_at=_iso_ago(hours=1), triggered_by="schedule")
        _seed(tmp_db, started_at=_iso_ago(hours=1), triggered_by="loop")
        out = ops.get_agent_analytics("agent-1", 24)
        today = out["timeline"][-1]
        assert today["by_type"].get("Chat/Tasks") == 1
        assert today["by_type"].get("Scheduled") == 1
        assert today["by_type"].get("Loops") == 1  # #1150
