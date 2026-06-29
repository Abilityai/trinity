"""Tests for agent report DB operations (#918).

Exercises ``ReportOperations`` against an ephemeral SQLite via the SQLAlchemy
Core engine (``db.engine.get_engine`` resolves ``TRINITY_DB_PATH``). Same fixture
machinery family as test_1115_schedules_summary.py.

Locked behaviour (from the issue AC + review hardening):
  * create → get round-trips the JSON payload; list is metadata-only (no payload).
  * fleet access filter: None = all (admin), list = IN-subset, empty list = [].
  * stats: total + by_type counts + distinct agents, honoring access + window.
  * delete is scoped by (agent_name, id) — a mismatched agent can't delete (Codex #2).
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
        CREATE TABLE agent_reports (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            user_id INTEGER,
            report_type TEXT NOT NULL,
            title TEXT NOT NULL,
            payload TEXT NOT NULL,
            display_hint TEXT,
            schema_version INTEGER DEFAULT 1,
            period_start TEXT,
            period_end TEXT,
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
        from db.reports import ReportOperations
    except ImportError:
        pytest.skip("backend venv required")
    return ReportOperations()


def _backdate(ops_obj, report_id: str, days: int) -> None:
    """Push a row's created_at into the past for retention/window tests."""
    from sqlalchemy import update
    from db.engine import get_engine
    from db.tables import agent_reports
    when = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with get_engine().begin() as conn:
        conn.execute(update(agent_reports).where(agent_reports.c.id == report_id).values(created_at=when))


def test_create_get_roundtrip(ops):
    rep = ops.create_report(
        agent_name="a1", user_id=7, report_type="recon.weekly_summary",
        title="Week 1", payload={"events": [{"label": "x"}]}, display_hint="timeline",
        schema_version=2, period_start="2026-01-01T00:00:00Z", period_end="2026-01-07T00:00:00Z",
    )
    got = ops.get_report(rep["id"])
    assert got["payload"] == {"events": [{"label": "x"}]}
    assert got["title"] == "Week 1"
    assert got["user_id"] == 7
    assert got["display_hint"] == "timeline"
    assert got["schema_version"] == 2


def test_list_is_metadata_only(ops):
    ops.create_report(agent_name="a1", user_id=1, report_type="ops.health",
                      title="H", payload={"secret": "should-not-be-in-summary"})
    rows = ops.get_reports_for_agent("a1")
    assert len(rows) == 1
    assert "payload" not in rows[0]
    assert rows[0]["title"] == "H"


def test_list_filters_by_type(ops):
    ops.create_report(agent_name="a1", user_id=1, report_type="ops.health", title="H", payload={})
    ops.create_report(agent_name="a1", user_id=1, report_type="recon.x", title="R", payload={})
    rows = ops.get_reports_for_agent("a1", report_type="ops.health")
    assert [r["report_type"] for r in rows] == ["ops.health"]


def test_fleet_access_filter(ops):
    ops.create_report(agent_name="a1", user_id=1, report_type="ops.health", title="H1", payload={})
    ops.create_report(agent_name="a2", user_id=1, report_type="ops.health", title="H2", payload={})
    # admin (None) sees all
    assert len(ops.get_fleet_reports(None)) == 2
    # subset
    rows = ops.get_fleet_reports(["a1"])
    assert {r["agent_name"] for r in rows} == {"a1"}
    # empty access set → nothing
    assert ops.get_fleet_reports([]) == []


def test_fleet_search(ops):
    ops.create_report(agent_name="a1", user_id=1, report_type="ops.health", title="Quarterly leads", payload={})
    ops.create_report(agent_name="a1", user_id=1, report_type="recon.x", title="Other", payload={})
    rows = ops.get_fleet_reports(None, search="leads")
    assert [r["title"] for r in rows] == ["Quarterly leads"]


def test_fleet_stats(ops):
    ops.create_report(agent_name="a1", user_id=1, report_type="ops.health", title="H", payload={})
    ops.create_report(agent_name="a1", user_id=1, report_type="ops.health", title="H2", payload={})
    ops.create_report(agent_name="a2", user_id=1, report_type="recon.x", title="R", payload={})
    stats = ops.get_fleet_report_stats(None)
    assert stats["total"] == 3
    assert stats["by_type"] == {"ops.health": 2, "recon.x": 1}
    assert stats["agents"] == 2
    # empty access set → zeros
    assert ops.get_fleet_report_stats([]) == {"total": 0, "by_type": {}, "agents": 0}


def test_delete_is_agent_scoped(ops):
    rep = ops.create_report(agent_name="a1", user_id=1, report_type="ops.health", title="H", payload={})
    # wrong agent cannot delete (Codex #2)
    assert ops.delete_report("a2", rep["id"]) is False
    assert ops.get_report(rep["id"]) is not None
    # correct agent deletes
    assert ops.delete_report("a1", rep["id"]) is True
    assert ops.get_report(rep["id"]) is None


def test_prune_respects_cutoff_and_disable(ops):
    old = ops.create_report(agent_name="a1", user_id=1, report_type="ops.health", title="old", payload={})
    new = ops.create_report(agent_name="a1", user_id=1, report_type="ops.health", title="new", payload={})
    _backdate(ops, old["id"], days=120)
    # disabled
    assert ops.prune_agent_reports(0) == 0
    assert ops.get_report(old["id"]) is not None
    # 90-day window deletes the 120-day-old row, keeps the new one
    assert ops.prune_agent_reports(90) == 1
    assert ops.get_report(old["id"]) is None
    assert ops.get_report(new["id"]) is not None
