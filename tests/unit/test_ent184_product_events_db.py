"""Tests for local product-event capture DB operations (ent#184).

Exercises ``ProductEventOperations`` against an ephemeral SQLite via the
SQLAlchemy Core engine (``db.engine.get_engine`` resolves ``TRINITY_DB_PATH``).
Same fixture family as test_918_agent_reports_db.py.

Locked behaviour (from the AC):
  * record → count_by_type / list round-trips the event + optional JSON context.
  * list is chronological (created_at ASC) so Tier-2 backfill can serialize
    history in order.
  * since-filter windows both counts and the list.
  * prune deletes past the cutoff and is disabled at 0.
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


def _make_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE product_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            installation_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_context TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


@pytest.fixture
def ops(tmp_path, monkeypatch):
    db_path = tmp_path / "trinity.db"
    conn = sqlite3.connect(str(db_path))
    _make_schema(conn)
    conn.close()
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    try:
        from db.product_events import ProductEventOperations
    except ImportError:
        pytest.skip("backend venv required")
    return ProductEventOperations()


def _backdate(event_id: int, days: int) -> None:
    from sqlalchemy import update
    from db.engine import get_engine
    from db.tables import product_events
    when = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with get_engine().begin() as conn:
        conn.execute(
            update(product_events).where(product_events.c.id == event_id).values(created_at=when)
        )


def test_record_roundtrips_context(ops):
    row = ops.record_product_event("inst-1", "setup_step_create", {"purpose": "research"})
    assert row["event_type"] == "setup_step_create"
    got = ops.list_product_events(event_type="setup_step_create")
    assert len(got) == 1
    assert got[0]["installation_id"] == "inst-1"
    assert got[0]["event_context"] == {"purpose": "research"}


def test_record_null_context(ops):
    ops.record_product_event("inst-1", "setup_started", None)
    got = ops.list_product_events()
    assert got[0]["event_context"] is None


def test_count_by_type(ops):
    ops.record_product_event("inst-1", "setup_started")
    ops.record_product_event("inst-1", "setup_started")
    ops.record_product_event("inst-1", "setup_completed")
    counts = ops.count_product_events_by_type()
    assert counts == {"setup_started": 2, "setup_completed": 1}


def test_list_is_chronological(ops):
    a = ops.record_product_event("inst-1", "setup_started")
    b = ops.record_product_event("inst-1", "setup_completed")
    _backdate(a["id"], 5)   # push the first one into the past
    _backdate(b["id"], 1)
    rows = ops.list_product_events()
    assert [r["event_type"] for r in rows] == ["setup_started", "setup_completed"]


def test_since_filter(ops):
    old = ops.record_product_event("inst-1", "setup_started")
    ops.record_product_event("inst-1", "setup_completed")  # "now"
    _backdate(old["id"], 40)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert ops.count_product_events_by_type(since=cutoff) == {"setup_completed": 1}
    assert len(ops.list_product_events(since=cutoff)) == 1


def test_prune(ops):
    old = ops.record_product_event("inst-1", "setup_started")
    ops.record_product_event("inst-1", "setup_completed")
    _backdate(old["id"], 100)
    # disabled at 0
    assert ops.prune_product_events(0) == 0
    assert len(ops.list_product_events()) == 2
    # 90-day window deletes the backdated row
    assert ops.prune_product_events(90) == 1
    remaining = ops.list_product_events()
    assert [r["event_type"] for r in remaining] == ["setup_completed"]
