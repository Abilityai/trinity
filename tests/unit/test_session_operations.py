"""
Unit tests for db.sessions.SessionOperations (Phase 1.7 of SESSION_TAB_2026-04).

Covers the CRUD round-trip, the Claude Code session-id cache, and the
resume-failure / resume-success counters that the Session tab turn endpoint
will rely on.

Pattern matches tests/unit/test_audit_retention_prune.py — tmp_path SQLite
+ monkeypatched TRINITY_DB_PATH + force-reload of db/connection.py so
SessionOperations talks to the isolated DB.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
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


_USERS_DDL = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    email TEXT,
    role TEXT NOT NULL DEFAULT 'user',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_AGENT_SESSIONS_DDL = """
CREATE TABLE agent_sessions (
    id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    user_email TEXT NOT NULL,
    started_at TEXT NOT NULL,
    last_message_at TEXT NOT NULL,
    message_count INTEGER DEFAULT 0,
    total_cost REAL DEFAULT 0.0,
    total_context_used INTEGER DEFAULT 0,
    total_context_max INTEGER DEFAULT 200000,
    status TEXT DEFAULT 'active',
    subscription_id TEXT,
    cached_claude_session_id TEXT,
    last_resume_at TEXT,
    consecutive_resume_failures INTEGER DEFAULT 0,
    compact_count INTEGER DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES users(id)
)
"""

_AGENT_SESSION_MESSAGES_DDL = """
CREATE TABLE agent_session_messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    user_email TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    cost REAL,
    context_used INTEGER,
    context_max INTEGER,
    cache_read_tokens INTEGER,
    tool_calls TEXT,
    execution_time_ms INTEGER,
    claude_session_id TEXT,
    compact_metadata TEXT,
    FOREIGN KEY (session_id) REFERENCES agent_sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id)
)
"""


@pytest.fixture
def session_ops(tmp_path, monkeypatch):
    """Build an isolated SQLite DB with the two session tables, return SessionOperations."""
    db_path = tmp_path / "trinity.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_path))

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_USERS_DDL)
    conn.executescript(_AGENT_SESSIONS_DDL)
    conn.executescript(_AGENT_SESSION_MESSAGES_DDL)
    conn.execute(
        "INSERT INTO users (username, email, role, created_at, updated_at) "
        "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
        ("alice", "alice@example.com", "user"),
    )
    conn.commit()
    conn.close()

    # Force-reload db/connection.py against the new TRINITY_DB_PATH.
    sys.modules.pop("_so_db_connection", None)
    _load("_so_db_connection", _BACKEND / "db" / "connection.py")

    db_pkg = type(sys)("db")
    db_pkg.__path__ = [str(_BACKEND / "db")]
    monkeypatch.setitem(sys.modules, "db", db_pkg)
    monkeypatch.setitem(sys.modules, "db.connection", sys.modules["_so_db_connection"])

    # SessionOperations imports `from db_models import AgentSession, AgentSessionMessage`.
    # db_models.py is at src/backend/db_models.py — already on sys.path via conftest.
    spec = importlib.util.spec_from_file_location(
        "db.sessions", str(_BACKEND / "db" / "sessions.py")
    )
    sessions_mod = importlib.util.module_from_spec(spec)
    sys.modules["db.sessions"] = sessions_mod
    spec.loader.exec_module(sessions_mod)
    return sessions_mod.SessionOperations()


def test_create_then_get_session(session_ops):
    s = session_ops.create_session("agentX", 1, "alice@example.com")

    assert s.id and len(s.id) >= 16  # urlsafe token, 16 random bytes
    assert s.agent_name == "agentX"
    assert s.user_id == 1
    assert s.user_email == "alice@example.com"
    assert s.message_count == 0
    assert s.total_cost == 0.0
    assert s.status == "active"
    assert s.cached_claude_session_id is None
    assert s.last_resume_at is None
    assert s.consecutive_resume_failures == 0

    fetched = session_ops.get_session(s.id)
    assert fetched is not None
    assert fetched.id == s.id
    assert fetched.agent_name == s.agent_name


def test_get_unknown_session_returns_none(session_ops):
    assert session_ops.get_session("does-not-exist") is None


def test_list_sessions_filters_and_orders(session_ops):
    a = session_ops.create_session("agentA", 1, "alice@example.com")
    b = session_ops.create_session("agentA", 1, "alice@example.com")
    other = session_ops.create_session("agentB", 1, "alice@example.com")

    # Touch a so its last_message_at moves to "now-after-b".
    session_ops.add_session_message(
        a.id, "agentA", 1, "alice@example.com", "user", "hi"
    )

    listed = session_ops.list_sessions("agentA", user_id=1)
    listed_ids = [s.id for s in listed]
    # a was the most recently touched -> first
    assert listed_ids[0] == a.id
    assert b.id in listed_ids
    assert other.id not in listed_ids  # filtered by agent_name

    # status filter
    only_active = session_ops.list_sessions("agentA", user_id=1, status="active")
    assert {s.id for s in only_active} == {a.id, b.id}

    none_archived = session_ops.list_sessions("agentA", user_id=1, status="archived")
    assert none_archived == []


def test_add_session_message_updates_aggregate(session_ops):
    s = session_ops.create_session("agentX", 1, "alice@example.com")

    user_msg = session_ops.add_session_message(
        s.id, "agentX", 1, "alice@example.com", "user", "hello"
    )
    assert user_msg.role == "user"
    assert user_msg.cost is None
    assert user_msg.cache_read_tokens is None

    asst_msg = session_ops.add_session_message(
        s.id, "agentX", 1, "alice@example.com",
        "assistant", "hi back",
        cost=0.0012, context_used=12000, context_max=200000,
        cache_read_tokens=11000,
        tool_calls='[{"name":"Read"}]',
        execution_time_ms=1234,
        claude_session_id="3abcc2e4-c815-4a71-ae40-caf49cb9d71f",
    )
    assert asst_msg.role == "assistant"
    assert asst_msg.cost == 0.0012
    assert asst_msg.cache_read_tokens == 11000
    assert asst_msg.tool_calls == '[{"name":"Read"}]'
    assert asst_msg.claude_session_id == "3abcc2e4-c815-4a71-ae40-caf49cb9d71f"

    refreshed = session_ops.get_session(s.id)
    assert refreshed.message_count == 2
    assert refreshed.total_cost == pytest.approx(0.0012)
    assert refreshed.total_context_used == 12000
    assert refreshed.total_context_max == 200000


def test_total_context_used_reflects_last_reading_not_watermark(session_ops):
    """
    `total_context_used` must mirror the most recent assistant turn's cache size,
    not a high-water mark. Claude Code auto-compacts mid-turn (~85% of the model
    window), which drops the per-turn cache reading sharply on the next turn —
    a watermark would asymptote near the compact threshold and stop conveying
    useful information.

    Asserts that a *lower* new value overwrites a *higher* prior value, plus that
    the existing total_context_max cap (cc5c37bc) still defends against accounting
    bugs reporting impossible token counts.
    """
    s = session_ops.create_session("agentX", 1, "alice@example.com")

    # Heavy assistant turn — pre-compact peak.
    session_ops.add_session_message(
        s.id, "agentX", 1, "alice@example.com",
        "assistant", "heavy",
        context_used=130_000, context_max=200_000,
    )
    assert session_ops.get_session(s.id).total_context_used == 130_000

    # Auto-compact fires; next turn's reading drops sharply.
    session_ops.add_session_message(
        s.id, "agentX", 1, "alice@example.com",
        "assistant", "post-compact",
        context_used=15_000, context_max=200_000,
    )
    refreshed = session_ops.get_session(s.id)
    assert refreshed.total_context_used == 15_000, (
        "total_context_used must reflect the latest reading, not a watermark"
    )

    # Cap at total_context_max still applies — defends against accounting bugs.
    session_ops.add_session_message(
        s.id, "agentX", 1, "alice@example.com",
        "assistant", "impossible",
        context_used=10_000_000, context_max=200_000,
    )
    assert session_ops.get_session(s.id).total_context_used == 200_000


def test_compact_metadata_persists_and_count_accumulates(session_ops):
    """A turn that observed N compact_boundary events stores the JSON list on
    the message row AND bumps the session's running compact_count tally —
    used by the inline reset-memory hint without scanning per-message rows.
    """
    import json as _json
    s = session_ops.create_session("agentX", 1, "alice@example.com")

    events_turn1 = [
        {"trigger": "auto", "pre_tokens": 170_325, "post_tokens": 12_691,
         "duration_ms": 110_361, "timestamp": "t1"},
    ]
    msg1 = session_ops.add_session_message(
        s.id, "agentX", 1, "alice@example.com",
        "assistant", "first heavy turn",
        compact_metadata=_json.dumps(events_turn1),
        compact_event_count=len(events_turn1),
    )
    assert msg1.compact_metadata == _json.dumps(events_turn1)
    assert session_ops.get_session(s.id).compact_count == 1

    # Turn 2: two compacts in one heavy turn (rare but legal).
    events_turn2 = [
        {"trigger": "auto", "pre_tokens": 169_341, "post_tokens": 9_763,
         "duration_ms": 123_195, "timestamp": "t2"},
        {"trigger": "auto", "pre_tokens": 167_261, "post_tokens": 14_298,
         "duration_ms": 113_181, "timestamp": "t3"},
    ]
    session_ops.add_session_message(
        s.id, "agentX", 1, "alice@example.com",
        "assistant", "second heavy turn",
        compact_metadata=_json.dumps(events_turn2),
        compact_event_count=len(events_turn2),
    )
    assert session_ops.get_session(s.id).compact_count == 3

    # Turn 3: no compact — message stores NULL, session counter stays.
    msg3 = session_ops.add_session_message(
        s.id, "agentX", 1, "alice@example.com",
        "assistant", "light turn",
    )
    assert msg3.compact_metadata is None
    assert session_ops.get_session(s.id).compact_count == 3


def test_total_context_used_unchanged_when_value_omitted(session_ops):
    """
    If a turn doesn't report context_used (e.g. the stdout-pipe-race recovered
    turn from Phase 5.1), the prior value must persist — we should not zero
    the column.
    """
    s = session_ops.create_session("agentX", 1, "alice@example.com")

    session_ops.add_session_message(
        s.id, "agentX", 1, "alice@example.com",
        "assistant", "first",
        context_used=42_000, context_max=200_000,
    )
    assert session_ops.get_session(s.id).total_context_used == 42_000

    # Turn lands without context_used (None) — the existing value should hold.
    session_ops.add_session_message(
        s.id, "agentX", 1, "alice@example.com",
        "assistant", "second",
        context_used=None, context_max=None,
    )
    assert session_ops.get_session(s.id).total_context_used == 42_000


def test_get_session_messages_returns_oldest_first(session_ops):
    s = session_ops.create_session("agentX", 1, "alice@example.com")
    for i in range(3):
        session_ops.add_session_message(
            s.id, "agentX", 1, "alice@example.com",
            "user" if i % 2 == 0 else "assistant",
            f"msg{i}",
        )
    msgs = session_ops.get_session_messages(s.id)
    assert [m.content for m in msgs] == ["msg0", "msg1", "msg2"]
    # limit narrows the recent window AND the result respects oldest-first
    msgs_two = session_ops.get_session_messages(s.id, limit=2)
    assert [m.content for m in msgs_two] == ["msg1", "msg2"]


def test_cached_claude_session_id_lifecycle(session_ops):
    s = session_ops.create_session("agentX", 1, "alice@example.com")

    assert session_ops.get_cached_claude_session_id(s.id) is None

    assert session_ops.update_cached_claude_session_id(
        s.id, "3abcc2e4-c815-4a71-ae40-caf49cb9d71f"
    )
    assert (
        session_ops.get_cached_claude_session_id(s.id)
        == "3abcc2e4-c815-4a71-ae40-caf49cb9d71f"
    )

    assert session_ops.clear_cached_claude_session_id(s.id)
    assert session_ops.get_cached_claude_session_id(s.id) is None


def test_resume_failure_and_success_counters(session_ops):
    s = session_ops.create_session("agentX", 1, "alice@example.com")

    n1 = session_ops.mark_resume_failure(s.id)
    assert n1 == 1
    n2 = session_ops.mark_resume_failure(s.id)
    assert n2 == 2

    # Snapshot timestamp was None before success.
    assert session_ops.get_session(s.id).last_resume_at is None

    assert session_ops.mark_resume_success(s.id)
    s_after = session_ops.get_session(s.id)
    assert s_after.consecutive_resume_failures == 0
    assert s_after.last_resume_at is not None


def test_delete_session_removes_messages(session_ops):
    s = session_ops.create_session("agentX", 1, "alice@example.com")
    session_ops.add_session_message(
        s.id, "agentX", 1, "alice@example.com", "user", "hi"
    )
    session_ops.add_session_message(
        s.id, "agentX", 1, "alice@example.com", "assistant", "back"
    )
    assert len(session_ops.get_session_messages(s.id)) == 2

    assert session_ops.delete_session(s.id)
    assert session_ops.get_session(s.id) is None
    assert session_ops.get_session_messages(s.id) == []


def test_delete_unknown_session_returns_false(session_ops):
    assert session_ops.delete_session("nope") is False
