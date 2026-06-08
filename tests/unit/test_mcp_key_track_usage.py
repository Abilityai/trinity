"""
Unit tests for the `track_usage` flag on db.mcp_keys.validate_mcp_api_key
(RELIABILITY-004 / #307, decision D3).

The agent heartbeat endpoint validates the agent's MCP key on every 5s beat.
Bumping `usage_count` on each heartbeat would amplify the counter into
meaninglessness and write to SQLite ~12x/min/agent. The fix parameterizes the
existing validator with `track_usage` (default True) so the heartbeat path can
opt out without forking the resolver.

R1 (mandatory regression): existing callers — which pass no flag — must still
bump `usage_count`. R2-style: `track_usage=False` leaves it untouched.

Pattern matches tests/unit/test_session_operations.py — tmp_path SQLite +
monkeypatched TRINITY_DB_PATH + force-reload of db/connection.py so
McpKeyOperations talks to the isolated DB.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"

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

_MCP_KEYS_DDL = """
CREATE TABLE mcp_api_keys (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    key_prefix TEXT NOT NULL,
    key_hash TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    usage_count INTEGER DEFAULT 0,
    is_active INTEGER DEFAULT 1,
    user_id INTEGER NOT NULL,
    agent_name TEXT,
    scope TEXT DEFAULT 'user',
    FOREIGN KEY (user_id) REFERENCES users(id)
)
"""

_TEST_KEY = "trinity_mcp_unit_test_key_value"

# The mcp_ops fixture loads db/connection.py + db/mcp_keys.py into sys.modules
# under real package names so McpKeyOperations talks to the isolated DB.
# Declared via the _STUBBED_MODULE_NAMES + _restore_sys_modules escape hatch so
# tests/lint_sys_modules.py permits the loader's sys.modules writes and the
# isolated-DB modules don't leak across test files. Precedent:
# tests/unit/test_telegram_webhook_backfill.py.
_STUBBED_MODULE_NAMES = [
    "_hb_db_connection",
    "db",
    "db.connection",
    "db.mcp_keys",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Snapshot the db entries the fixture mutates; restore after each test."""
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mcp_ops(tmp_path, monkeypatch):
    """Isolated SQLite DB with users + mcp_api_keys, one agent-scoped key.

    Returns (McpKeyOperations instance, db_path).
    """
    db_path = tmp_path / "trinity.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_path))

    key_hash = hashlib.sha256(_TEST_KEY.encode()).hexdigest()

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_USERS_DDL)
    conn.executescript(_MCP_KEYS_DDL)
    conn.execute(
        "INSERT INTO users (username, email, role, created_at, updated_at) "
        "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
        ("alice", "alice@example.com", "user"),
    )
    conn.execute(
        "INSERT INTO mcp_api_keys "
        "(id, name, description, key_prefix, key_hash, created_at, "
        " usage_count, is_active, user_id, agent_name, scope) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'), 0, 1, 1, ?, 'agent')",
        ("k1", "agent-key", None, "trinity_mcp_", key_hash, "my-agent"),
    )
    conn.commit()
    conn.close()

    # Force-reload db/connection.py against the new TRINITY_DB_PATH.
    sys.modules.pop("_hb_db_connection", None)
    _load("_hb_db_connection", _BACKEND / "db" / "connection.py")

    db_pkg = type(sys)("db")
    db_pkg.__path__ = [str(_BACKEND / "db")]
    monkeypatch.setitem(sys.modules, "db", db_pkg)
    monkeypatch.setitem(sys.modules, "db.connection", sys.modules["_hb_db_connection"])

    spec = importlib.util.spec_from_file_location(
        "db.mcp_keys", str(_BACKEND / "db" / "mcp_keys.py")
    )
    mcp_mod = importlib.util.module_from_spec(spec)
    sys.modules["db.mcp_keys"] = mcp_mod
    spec.loader.exec_module(mcp_mod)
    # validate_mcp_api_key does not touch user_ops; pass a placeholder.
    return mcp_mod.McpKeyOperations(user_ops=None), db_path


def _usage_count(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute("SELECT usage_count FROM mcp_api_keys WHERE id = 'k1'")
        return cur.fetchone()[0]
    finally:
        conn.close()


def test_validate_default_bumps_usage_count(mcp_ops):
    """R1 regression: the default (no flag) path still increments usage_count."""
    ops, db_path = mcp_ops
    assert _usage_count(db_path) == 0

    res = ops.validate_mcp_api_key(_TEST_KEY)

    assert res is not None
    assert res["scope"] == "agent"
    assert res["agent_name"] == "my-agent"
    assert _usage_count(db_path) == 1


def test_validate_track_usage_false_does_not_bump(mcp_ops):
    """track_usage=False resolves the key but leaves usage_count untouched."""
    ops, db_path = mcp_ops
    assert _usage_count(db_path) == 0

    res = ops.validate_mcp_api_key(_TEST_KEY, track_usage=False)

    assert res is not None
    assert res["scope"] == "agent"
    assert res["agent_name"] == "my-agent"
    # Heartbeat amplification guard: counter must not move.
    assert _usage_count(db_path) == 0


def test_validate_track_usage_false_repeated_calls_stay_zero(mcp_ops):
    """Simulate a minute of heartbeats: usage_count never moves."""
    ops, db_path = mcp_ops
    for _ in range(12):
        ops.validate_mcp_api_key(_TEST_KEY, track_usage=False)
    assert _usage_count(db_path) == 0


def test_validate_invalid_key_returns_none(mcp_ops):
    ops, _ = mcp_ops
    assert ops.validate_mcp_api_key("nope", track_usage=False) is None
