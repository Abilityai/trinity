"""
Unit tests for audit_log retention prune (#552).

Covers ``PlatformAuditOperations.prune_audit_log`` — the service-level
scheduler is a thin APScheduler wrapper around it.

What we pin:
- Old rows (> retention) are removed.
- Young rows are kept (the SQLite ``audit_log_no_delete`` trigger
  protects them; if prune ever tries, sqlite raises and the test
  fails loudly).
- ``retention_days < 365`` is rejected before touching the DB.
- Empty table prune returns 0.
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


_AUDIT_LOG_DDL = """
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,
    event_type TEXT NOT NULL,
    event_action TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    actor_id TEXT,
    actor_email TEXT,
    actor_ip TEXT,
    mcp_key_id TEXT,
    mcp_key_name TEXT,
    mcp_scope TEXT,
    target_type TEXT,
    target_id TEXT,
    timestamp TEXT NOT NULL,
    details TEXT,
    request_id TEXT,
    source TEXT NOT NULL,
    endpoint TEXT,
    previous_hash TEXT,
    entry_hash TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_NO_UPDATE_TRIGGER = """
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'Audit log entries cannot be modified');
END
"""

_NO_DELETE_TRIGGER = """
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
WHEN OLD.timestamp > datetime('now', '-365 days')
BEGIN
    SELECT RAISE(ABORT, 'Audit log entries cannot be deleted within retention period');
END
"""


@pytest.fixture
def audit_ops(tmp_path, monkeypatch):
    """Build a tmp DB with the audit_log table + triggers, return ``PlatformAuditOperations``."""
    db_path = tmp_path / "trinity.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_path))

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_AUDIT_LOG_DDL)
    conn.executescript(_NO_UPDATE_TRIGGER)
    conn.executescript(_NO_DELETE_TRIGGER)
    conn.commit()
    conn.close()

    # Force-reload db/connection.py so it picks up the new TRINITY_DB_PATH.
    sys.modules.pop("_arp_db_connection", None)
    _load("_arp_db_connection", _BACKEND / "db" / "connection.py")

    db_pkg = type(sys)("db")
    db_pkg.__path__ = [str(_BACKEND / "db")]
    monkeypatch.setitem(sys.modules, "db", db_pkg)
    monkeypatch.setitem(sys.modules, "db.connection", sys.modules["_arp_db_connection"])

    # Load db/audit.py as `db.audit` so its relative `from .connection import ...`
    # resolves against the `db` package we just registered.
    spec = importlib.util.spec_from_file_location(
        "db.audit", str(_BACKEND / "db" / "audit.py")
    )
    audit_mod = importlib.util.module_from_spec(spec)
    # Restored at teardown (#3390): left in place, this private copy and the real
    # package's `db.audit` attribute were two modules for the rest of the run.
    monkeypatch.setitem(sys.modules, "db.audit", audit_mod)
    spec.loader.exec_module(audit_mod)
    return audit_mod.PlatformAuditOperations()


def _insert(db_path: Path, *, event_id: str, days_ago: int) -> None:
    """Insert a minimal audit_log row whose timestamp is ``days_ago`` days old."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO audit_log
            (event_id, event_type, event_action, actor_type,
             timestamp, source, created_at)
        VALUES (?, ?, ?, ?, datetime('now', ?), ?, datetime('now'))
        """,
        (event_id, "test", "noop", "system", f"-{days_ago} days", "api"),
    )
    conn.commit()
    conn.close()


def _count(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    n = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    conn.close()
    return int(n)


def test_prune_removes_only_old_rows(audit_ops, tmp_path):
    db_path = tmp_path / "trinity.db"

    _insert(db_path, event_id="old-400d", days_ago=400)
    _insert(db_path, event_id="old-380d", days_ago=380)
    _insert(db_path, event_id="recent-30d", days_ago=30)
    _insert(db_path, event_id="recent-1d", days_ago=1)
    assert _count(db_path) == 4

    removed = audit_ops.prune_audit_log(365)

    assert removed == 2
    assert _count(db_path) == 2

    # Confirm the survivors are the recent ones.
    conn = sqlite3.connect(str(db_path))
    rows = {r[0] for r in conn.execute("SELECT event_id FROM audit_log").fetchall()}
    conn.close()
    assert rows == {"recent-30d", "recent-1d"}


def test_prune_empty_table_returns_zero(audit_ops):
    assert audit_ops.prune_audit_log(365) == 0


def test_prune_below_floor_raises(audit_ops, tmp_path):
    db_path = tmp_path / "trinity.db"
    _insert(db_path, event_id="anything", days_ago=10)

    with pytest.raises(ValueError, match="retention_days must be >= 365"):
        audit_ops.prune_audit_log(364)

    # No rows touched.
    assert _count(db_path) == 1


def test_prune_does_not_violate_no_delete_trigger(audit_ops, tmp_path):
    """The WHERE clause and trigger WHEN clause must agree on the cutoff —
    otherwise sqlite raises an IntegrityError on boundary rows."""
    db_path = tmp_path / "trinity.db"

    # Seed many rows clustered around the cutoff. If prune ever picks a row
    # the trigger considers protected, sqlite raises and rowcount is -1
    # (or the entire DELETE aborts). Test fails loudly on either path.
    for d in (366, 380, 400, 720):
        _insert(db_path, event_id=f"old-{d}", days_ago=d)
    for d in (10, 100, 200, 360):
        _insert(db_path, event_id=f"new-{d}", days_ago=d)

    removed = audit_ops.prune_audit_log(365)
    assert removed == 4
    assert _count(db_path) == 4
