"""Tests for per-schedule performance rollups (#1115).

Exercises `ScheduleOperations.get_agent_schedules_summary` against an
ephemeral SQLite. Same fixture machinery as `test_agent_analytics.py` (#1107)
and `test_schedule_analytics.py` (#868) — `db.connection.DB_PATH` monkeypatched
at a tmp file, stale sibling `db.*` stubs popped from `sys.modules`.

Locked behaviour (from the issue's AC):
  * ONE rollup row per non-deleted schedule; a zero-run schedule still appears.
  * `success_rate` terminal-based: success / (success + failed [incl. `error`]);
    `None` when zero terminal runs (UI shows `—`, not a false 0%).
  * `avg_duration_ms` NULL-skipping; `tool_call_total` parsed from tool_calls JSON.
  * Soft-deleted schedules (`deleted_at` set) are excluded.
  * Window honored via iso_cutoff — rows outside the window don't count.
"""
from __future__ import annotations

import json
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


_STUBBED_MODULE_NAMES = ["db.schedules", "db.users", "db.agents", "db.monitoring"]


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


def _make_db_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
            password_hash TEXT, role TEXT DEFAULT 'user',
            auth0_sub TEXT, name TEXT, picture TEXT, email TEXT,
            created_at TEXT, updated_at TEXT, last_login TEXT
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
            created_at TEXT NOT NULL,
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
            context_used INTEGER,
            tool_calls TEXT,
            triggered_by TEXT NOT NULL DEFAULT 'schedule',
            message TEXT NOT NULL DEFAULT ''
        )
        """
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
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes, hours=hours, days=days)
    return when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _add_schedule(db_path, sid, agent="agent-1", name="S", message="/do-it",
                  cron="*/5 * * * *", enabled=1, deleted_at=None):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO agent_schedules(id, agent_name, name, cron_expression, message, "
        "enabled, created_at, deleted_at) VALUES (?,?,?,?,?,?,?,?)",
        (sid, agent, name, cron, message, enabled, _iso_ago(days=1), deleted_at),
    )
    conn.commit()
    conn.close()


_seq = [0]


def _add_exec(db_path, sid, agent="agent-1", *, status="success", started_at=None,
              duration_ms=1000, cost=0.0, context_used=None, tool_calls=None):
    if started_at is None:
        started_at = _iso_ago(minutes=5)
    _seq[0] += 1
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO schedule_executions(id, schedule_id, agent_name, status, started_at, "
        "duration_ms, cost, context_used, tool_calls) VALUES (?,?,?,?,?,?,?,?,?)",
        (f"x-{_seq[0]}", sid, agent, status, started_at, duration_ms, cost,
         context_used, tool_calls),
    )
    conn.commit()
    conn.close()


# ----------------------------------------------------------------------

def test_success_rate_is_terminal_based(tmp_db, ops):
    _add_schedule(tmp_db, "s1")
    # 3 success, 1 failed, 1 error → terminal=5, rate = 3/5 = 0.6.
    # 1 running is NON-terminal: counted in total but NOT in the rate.
    for _ in range(3):
        _add_exec(tmp_db, "s1", status="success", duration_ms=1000)
    _add_exec(tmp_db, "s1", status="failed", duration_ms=500)
    _add_exec(tmp_db, "s1", status="error", duration_ms=None)
    _add_exec(tmp_db, "s1", status="running", duration_ms=None)

    out = ops.get_agent_schedules_summary("agent-1", 168)
    row = out["schedules"][0]
    assert out["schedule_count"] == 1
    assert row["total_executions"] == 6
    assert row["success_count"] == 3
    assert row["failed_count"] == 2          # failed + error
    assert row["success_rate"] == 0.6        # 3 / (3 + 2)
    assert row["command"] == "/do-it"


def test_avg_duration_skips_nulls(tmp_db, ops):
    _add_schedule(tmp_db, "s1")
    _add_exec(tmp_db, "s1", status="success", duration_ms=200)
    _add_exec(tmp_db, "s1", status="success", duration_ms=400)
    _add_exec(tmp_db, "s1", status="failed", duration_ms=None)  # NULL skipped
    out = ops.get_agent_schedules_summary("agent-1", 168)
    assert out["schedules"][0]["avg_duration_ms"] == 300        # (200+400)/2


def test_tool_call_total_parsed_from_json(tmp_db, ops):
    _add_schedule(tmp_db, "s1")
    _add_exec(tmp_db, "s1", tool_calls=json.dumps([{"name": "Bash"}, {"name": "Read"}]))
    _add_exec(tmp_db, "s1", tool_calls=json.dumps([{"tool": "Grep"}]))
    _add_exec(tmp_db, "s1", tool_calls="not json")   # malformed → skipped
    _add_exec(tmp_db, "s1", tool_calls=None)         # none → skipped
    out = ops.get_agent_schedules_summary("agent-1", 168)
    assert out["schedules"][0]["tool_call_total"] == 3


def test_zero_run_schedule_still_appears(tmp_db, ops):
    _add_schedule(tmp_db, "s1", name="busy")
    _add_schedule(tmp_db, "s2", name="idle")
    _add_exec(tmp_db, "s1", status="success")
    out = ops.get_agent_schedules_summary("agent-1", 168)
    by_id = {r["schedule_id"]: r for r in out["schedules"]}
    assert set(by_id) == {"s1", "s2"}
    idle = by_id["s2"]
    assert idle["total_executions"] == 0
    assert idle["success_rate"] is None          # zero terminal → None, not 0.0
    assert idle["avg_duration_ms"] is None
    assert idle["tool_call_total"] == 0
    assert idle["last_run_status"] is None


def test_soft_deleted_schedule_excluded(tmp_db, ops):
    _add_schedule(tmp_db, "live")
    _add_schedule(tmp_db, "gone", deleted_at=_iso_ago(hours=1))
    _add_exec(tmp_db, "live", status="success")
    _add_exec(tmp_db, "gone", status="success")
    out = ops.get_agent_schedules_summary("agent-1", 168)
    assert {r["schedule_id"] for r in out["schedules"]} == {"live"}


def test_out_of_window_runs_excluded(tmp_db, ops):
    _add_schedule(tmp_db, "s1")
    _add_exec(tmp_db, "s1", status="success", started_at=_iso_ago(minutes=30))
    _add_exec(tmp_db, "s1", status="failed", started_at=_iso_ago(days=10))  # outside 7d
    out = ops.get_agent_schedules_summary("agent-1", 168)
    row = out["schedules"][0]
    assert row["total_executions"] == 1
    assert row["success_rate"] == 1.0
