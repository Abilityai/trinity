"""#1474 — backend read boundaries normalize naive timestamps to UTC 'Z'.

Cron-fired executions were stored with naive timestamps (no 'Z'). The
summary/list readers returned raw ``dict(row)`` values, so the naive string
serialized straight through Pydantic and JS ``new Date(naive)`` parsed it as
local time — shifting schedule-triggered rows by the viewer's UTC offset.

These tests pin the leaking read boundaries (the layer verify-local's unit
stage runs) so a seeded *naive* row comes back UTC-normalized:
  * ``get_agent_executions_summary``  → TasksPanel (`/api/agents/{name}/executions`)
  * ``get_fleet_executions``          → ExecutionsPanel (`/api/executions`)
  * ``get_agent_schedules_summary``   → Overview/Schedules `last_run_at`
  * ``_row_to_activity`` / ``_mapping_to_activity`` → UnifiedActivity
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


_STUBBED_MODULE_NAMES = ["db.schedules", "db.activities", "db.users", "db.agents"]


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


# A naive scheduler-written timestamp (no 'Z') and its aware equivalent.
# Derived from now − 1h, NOT a fixed literal: the schedules-summary read is
# 168h-windowed, so a literal date rots out of the window and the test becomes
# a time bomb (#1783 — fired first on the v0.8.5 release PR).
INSTANT = datetime.now(timezone.utc).replace(microsecond=207634) - timedelta(hours=1)
NAIVE = INSTANT.replace(tzinfo=None).isoformat()


def _assert_utc_normalized(value: str):
    """The returned string carries an explicit UTC marker (not naive) and
    preserves the same instant."""
    assert value is not None
    assert value.endswith("Z") or value.endswith("+00:00"), value
    # parse back and confirm it is the same UTC instant
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed == INSTANT


def _make_exec_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE schedule_executions (
            id TEXT PRIMARY KEY,
            schedule_id TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            duration_ms INTEGER,
            message TEXT NOT NULL DEFAULT '',
            response TEXT,
            error TEXT,
            triggered_by TEXT NOT NULL DEFAULT 'schedule',
            context_used INTEGER,
            context_max INTEGER,
            cost REAL,
            tool_calls TEXT,
            execution_log TEXT,
            claude_session_id TEXT,
            source_user_id INTEGER,
            source_user_email TEXT,
            source_agent_name TEXT,
            source_mcp_key_id TEXT,
            source_mcp_key_name TEXT,
            model_used TEXT,
            fan_out_id TEXT,
            business_status TEXT,
            validation_execution_id TEXT,
            turn_integrity TEXT,
            queued_at TEXT,
            source_channel TEXT,
            source_channel_chat_id TEXT,
            loop_id TEXT
        )
        """
    )
    conn.execute(
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
    conn.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
            password_hash TEXT, role TEXT DEFAULT 'user',
            auth0_sub TEXT, name TEXT, picture TEXT, email TEXT,
            created_at TEXT, updated_at TEXT, last_login TEXT
        )
        """
    )
    conn.execute("INSERT INTO users(id, username, role) VALUES (1, 'owner', 'user')")


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "trinity.db"
    conn = sqlite3.connect(str(db_path))
    _make_exec_schema(conn)
    # One execution row with a NAIVE started_at + completed_at (the bug shape).
    conn.execute(
        "INSERT INTO schedule_executions(id, schedule_id, agent_name, status, "
        "started_at, completed_at, queued_at, duration_ms, message, triggered_by) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("e1", "s1", "agent-1", "success", NAIVE, NAIVE, NAIVE, 100, "m", "schedule"),
    )
    # A schedule + its most-recent run (drives last_run_at in the summary).
    conn.execute(
        "INSERT INTO agent_schedules(id, agent_name, name, cron_expression, message, "
        "enabled, created_at) VALUES (?,?,?,?,?,?,?)",
        ("s1", "agent-1", "S", "*/5 * * * *", "/do-it", 1, NAIVE),
    )
    conn.commit()
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


# --------------------------------------------------------------------------

def test_agent_executions_summary_normalizes_naive(ops):
    rows = ops.get_agent_executions_summary("agent-1", limit=50)
    assert len(rows) == 1
    _assert_utc_normalized(rows[0]["started_at"])
    _assert_utc_normalized(rows[0]["completed_at"])


def test_fleet_executions_normalizes_naive(ops):
    rows = ops.get_fleet_executions(None, hours=0)  # admin, all-time
    assert len(rows) == 1
    _assert_utc_normalized(rows[0]["started_at"])
    _assert_utc_normalized(rows[0]["completed_at"])
    _assert_utc_normalized(rows[0]["queued_at"])


def test_schedules_summary_last_run_at_normalized(ops):
    out = ops.get_agent_schedules_summary("agent-1", 168)
    row = out["schedules"][0]
    _assert_utc_normalized(row["last_run_at"])


def test_norm_ts_none_and_empty_pass_through(tmp_db):
    """A NULL/empty timestamp must stay None (no crash, no phantom value)."""
    try:
        from db.schedules import _norm_ts
    except ImportError:
        pytest.skip("backend venv required")
    assert _norm_ts(None) is None
    assert _norm_ts("") is None


# --------------------------------------------------------------------------
# Activity mappers (static — no DB needed)

def test_row_to_activity_normalizes_naive():
    try:
        from db.activities import ActivityOperations
    except ImportError:
        pytest.skip("backend venv required")
    # positional row: [0..14] per _ACTIVITY_COLUMNS DDL order
    row = [
        "a1", "agent-1", "schedule_start", "started", None,
        NAIVE, NAIVE, 100, 1, "schedule",
        None, "e1", None, None, NAIVE,
    ]
    out = ActivityOperations._row_to_activity(row)
    _assert_utc_normalized(out["started_at"])
    _assert_utc_normalized(out["completed_at"])
    _assert_utc_normalized(out["created_at"])


def test_mapping_to_activity_normalizes_naive():
    try:
        from db.activities import ActivityOperations
    except ImportError:
        pytest.skip("backend venv required")
    row = {
        "id": "a1", "agent_name": "agent-1", "activity_type": "schedule_end",
        "activity_state": "completed", "parent_activity_id": None,
        "started_at": NAIVE, "completed_at": NAIVE, "duration_ms": 100,
        "user_id": 1, "triggered_by": "schedule", "related_chat_message_id": None,
        "related_execution_id": "e1", "details": None, "error": None,
        "created_at": NAIVE,
    }
    out = ActivityOperations._mapping_to_activity(row)
    _assert_utc_normalized(out["started_at"])
    _assert_utc_normalized(out["completed_at"])
    _assert_utc_normalized(out["created_at"])
