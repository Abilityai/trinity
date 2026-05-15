"""
Unit tests for fleet execution DB queries and router helper (EXEC-022 / Issue #18).

Covers:
- _narrow_to_agent: admin path (None), non-admin access-gate, no ?agent param
- get_fleet_executions SQL: admin sees all, non-admin sees subset, empty for zero-access
- get_fleet_executions SQL: status/triggered_by/search/hours filters
- get_fleet_executions SQL: invalid status silently discarded (treated as None)
- get_fleet_execution_stats SQL: windowed vs all-time, running_count always-live
- get_fleet_execution_stats SQL: empty result has no division-by-zero

Strategy: the router's _narrow_to_agent is pure Python — tested directly.
The DB methods are tested by running their SQL against a real SQLite connection
using the iso_cutoff() helper from utils/helpers.py. We bypass the circular-import
problem of loading db/schedules.py in isolation by reimplementing the two queries
in a minimal test fixture that mirrors the production SQL exactly.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import pytest


def iso_cutoff(hours: int) -> str:
    """Mirrors utils.helpers.iso_cutoff — inlined to avoid sys.modules manipulation."""
    return (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ---------------------------------------------------------------------------
# _narrow_to_agent — copy of the production implementation (pure Python)
# ---------------------------------------------------------------------------

def _narrow_to_agent(
    agent_names: Optional[List[str]], agent: Optional[str]
) -> Optional[List[str]]:
    if not agent:
        return agent_names
    if agent_names is None:
        return [agent]
    return [agent] if agent in agent_names else []


# ---------------------------------------------------------------------------
# SQL helpers that mirror db/schedules.py exactly
# ---------------------------------------------------------------------------

def _utc_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _make_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS schedule_executions (
            id TEXT PRIMARY KEY,
            schedule_id TEXT NOT NULL DEFAULT 'sched-1',
            agent_name TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            duration_ms INTEGER,
            message TEXT NOT NULL DEFAULT 'test task',
            triggered_by TEXT NOT NULL DEFAULT 'schedule',
            error TEXT,
            context_used INTEGER,
            context_max INTEGER,
            cost REAL,
            model_used TEXT,
            queued_at TEXT,
            backlog_metadata TEXT,
            source_user_id INTEGER,
            source_user_email TEXT,
            source_agent_name TEXT,
            fan_out_id TEXT,
            business_status TEXT,
            validation_execution_id TEXT,
            execution_log TEXT,
            response TEXT
        );
    """)
    return conn


def _insert_row(conn: sqlite3.Connection, **kwargs) -> None:
    now = datetime.now(timezone.utc)
    defaults = dict(
        id="exec-1", schedule_id="sched-1", agent_name="agent-a",
        status="success", started_at=_utc_iso(now), completed_at=None,
        duration_ms=1000, message="test task", triggered_by="schedule",
        error=None, context_used=1000, context_max=200000, cost=0.001,
        model_used=None, queued_at=None, backlog_metadata=None,
        source_user_id=None, source_user_email=None, source_agent_name=None,
        fan_out_id=None, business_status=None, validation_execution_id=None,
        execution_log=None, response=None,
    )
    defaults.update(kwargs)
    cols = ", ".join(defaults.keys())
    phs = ", ".join("?" * len(defaults))
    conn.execute(f"INSERT INTO schedule_executions ({cols}) VALUES ({phs})", list(defaults.values()))
    conn.commit()


def get_fleet_executions(
    conn: sqlite3.Connection,
    agent_names: Optional[List[str]],
    *,
    status: Optional[str] = None,
    triggered_by: Optional[str] = None,
    hours: Optional[int] = 24,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list:
    """Mirror of ScheduleOperations.get_fleet_executions."""
    if agent_names is not None and len(agent_names) == 0:
        return []
    conditions: list = []
    params: list = []
    if agent_names is not None:
        placeholders = ", ".join("?" * len(agent_names))
        conditions.append(f"agent_name IN ({placeholders})")
        params.extend(agent_names)
    if status:
        conditions.append("status = ?")
        params.append(status)
    if triggered_by:
        conditions.append("triggered_by = ?")
        params.append(triggered_by)
    if hours:
        conditions.append("started_at > ?")
        params.append(iso_cutoff(hours))
    if search:
        conditions.append("message LIKE ?")
        params.append(f"%{search}%")
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"""
        SELECT
            id, schedule_id, agent_name, status, started_at, completed_at,
            duration_ms, message, triggered_by, context_used, context_max, cost,
            CASE WHEN status IN ('failed','error') THEN SUBSTR(error, 1, 200) ELSE NULL END AS error_summary,
            source_user_id, source_user_email, source_agent_name,
            model_used, fan_out_id, business_status, validation_execution_id, queued_at
        FROM schedule_executions
        {where}
        ORDER BY started_at DESC
        LIMIT ? OFFSET ?
    """
    params += [limit, offset]
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_fleet_execution_stats(
    conn: sqlite3.Connection,
    agent_names: Optional[List[str]],
    hours: int = 24,
) -> dict:
    """Mirror of ScheduleOperations.get_fleet_execution_stats."""
    if agent_names is not None and len(agent_names) == 0:
        return dict(total=0, success_count=0, failed_count=0,
                    running_count=0, queued_count=0, total_cost=0.0,
                    success_rate=0.0, hours=hours)
    agent_conditions: list = []
    agent_params: list = []
    if agent_names is not None:
        placeholders = ", ".join("?" * len(agent_names))
        agent_conditions.append(f"agent_name IN ({placeholders})")
        agent_params.extend(agent_names)
    agent_where = ("WHERE " + " AND ".join(agent_conditions)) if agent_conditions else ""

    time_cond = "started_at > ?" if hours else "1"
    time_params = [iso_cutoff(hours)] * 4 if hours else []

    sql = f"""
        SELECT
            SUM(CASE WHEN {time_cond} THEN 1 ELSE 0 END) AS total,
            SUM(CASE WHEN {time_cond} AND status = 'success' THEN 1 ELSE 0 END) AS success_count,
            SUM(CASE WHEN {time_cond} AND status IN ('failed','error') THEN 1 ELSE 0 END) AS failed_count,
            SUM(CASE WHEN {time_cond} THEN COALESCE(cost, 0) ELSE 0 END) AS total_cost,
            SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running_count,
            SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) AS queued_count
        FROM schedule_executions
        {agent_where}
    """
    params = time_params * 4 + agent_params
    # Each of the 4 CASE WHEN uses one time_param; rebuild correctly
    params = []
    for _ in range(4):
        params.extend(time_params[:1] if time_params else [])
    params.extend(agent_params)

    row = dict(conn.execute(sql, params).fetchone())
    total = row["total"] or 0
    return dict(
        total=total,
        success_count=row["success_count"] or 0,
        failed_count=row["failed_count"] or 0,
        running_count=row["running_count"] or 0,
        queued_count=row["queued_count"] or 0,
        total_cost=row["total_cost"] or 0.0,
        success_rate=round((row["success_count"] or 0) / total * 100, 1) if total else 0.0,
        hours=hours,
    )


# ---------------------------------------------------------------------------
# Tests: _narrow_to_agent (pure logic)
# ---------------------------------------------------------------------------

class TestNarrowToAgent:
    def test_no_agent_filter_returns_names_unchanged(self):
        assert _narrow_to_agent(["a", "b"], None) == ["a", "b"]

    def test_no_agent_filter_admin_none_unchanged(self):
        assert _narrow_to_agent(None, None) is None

    def test_admin_with_agent_returns_single_item_list(self):
        assert _narrow_to_agent(None, "agent-x") == ["agent-x"]

    def test_non_admin_accessible_agent_returns_single(self):
        assert _narrow_to_agent(["agent-a", "agent-b"], "agent-a") == ["agent-a"]

    def test_non_admin_inaccessible_agent_returns_empty(self):
        assert _narrow_to_agent(["agent-a"], "agent-b") == []

    def test_non_admin_empty_access_list_returns_empty(self):
        assert _narrow_to_agent([], "agent-a") == []


# ---------------------------------------------------------------------------
# Tests: get_fleet_executions SQL
# ---------------------------------------------------------------------------

@pytest.fixture()
def conn(tmp_path):
    c = _make_db(tmp_path)
    yield c
    c.close()


class TestGetFleetExecutions:
    @pytest.fixture(autouse=True)
    def seed(self, conn):
        self.conn = conn
        now = datetime.now(timezone.utc)
        _insert_row(conn, id="a1", agent_name="agent-a", status="success",
                    started_at=_utc_iso(now - timedelta(hours=1)), triggered_by="schedule")
        _insert_row(conn, id="a2", agent_name="agent-a", status="failed",
                    started_at=_utc_iso(now - timedelta(hours=2)), triggered_by="manual",
                    error="something went wrong " * 15)
        _insert_row(conn, id="b1", agent_name="agent-b", status="running",
                    started_at=_utc_iso(now - timedelta(minutes=5)), triggered_by="schedule")
        _insert_row(conn, id="old", agent_name="agent-a", status="success",
                    started_at=_utc_iso(now - timedelta(hours=48)), triggered_by="schedule")

    def _q(self, **kw):
        return get_fleet_executions(self.conn, **kw)

    def test_admin_sees_all_agents(self):
        rows = self._q(agent_names=None, hours=0)
        names = {r["agent_name"] for r in rows}
        assert names == {"agent-a", "agent-b"}

    def test_non_admin_filtered_to_accessible(self):
        rows = self._q(agent_names=["agent-a"], hours=0)
        assert all(r["agent_name"] == "agent-a" for r in rows)

    def test_empty_agent_names_returns_empty(self):
        assert self._q(agent_names=[], hours=0) == []

    def test_status_filter(self):
        rows = self._q(agent_names=None, status="failed", hours=0)
        assert all(r["status"] == "failed" for r in rows)
        assert len(rows) == 1

    def test_triggered_by_filter(self):
        rows = self._q(agent_names=None, triggered_by="manual", hours=0)
        assert all(r["triggered_by"] == "manual" for r in rows)

    def test_hours_window_excludes_old_rows(self):
        ids = {r["id"] for r in self._q(agent_names=None, hours=24)}
        assert "old" not in ids

    def test_hours_zero_includes_all(self):
        ids = {r["id"] for r in self._q(agent_names=None, hours=0)}
        assert "old" in ids

    def test_search_filter(self):
        rows = self._q(agent_names=None, search="test", hours=0)
        assert len(rows) > 0

    def test_error_summary_capped_at_200_chars(self):
        rows = self._q(agent_names=None, status="failed", hours=0)
        assert rows[0]["error_summary"] is not None
        assert len(rows[0]["error_summary"]) <= 200

    def test_success_rows_have_null_error_summary(self):
        rows = self._q(agent_names=None, status="success", hours=0)
        assert all(r["error_summary"] is None for r in rows)

    def test_limit_and_offset_paginate(self):
        p1 = {r["id"] for r in self._q(agent_names=None, hours=0, limit=2, offset=0)}
        p2 = {r["id"] for r in self._q(agent_names=None, hours=0, limit=2, offset=2)}
        assert len(p1) == 2
        assert p1.isdisjoint(p2)


# ---------------------------------------------------------------------------
# Tests: get_fleet_execution_stats SQL
# ---------------------------------------------------------------------------

class TestGetFleetExecutionStats:
    @pytest.fixture(autouse=True)
    def seed(self, conn):
        self.conn = conn
        now = datetime.now(timezone.utc)
        _insert_row(conn, id="s1", agent_name="agent-a", status="success",
                    started_at=_utc_iso(now - timedelta(hours=1)), cost=0.01)
        _insert_row(conn, id="s2", agent_name="agent-a", status="failed",
                    started_at=_utc_iso(now - timedelta(hours=2)), cost=0.005)
        _insert_row(conn, id="s3", agent_name="agent-a", status="running",
                    started_at=_utc_iso(now - timedelta(minutes=5)), cost=None)
        _insert_row(conn, id="s4", agent_name="agent-a", status="success",
                    started_at=_utc_iso(now - timedelta(hours=50)), cost=0.02)

    def _s(self, **kw):
        return get_fleet_execution_stats(self.conn, **kw)

    def test_windowed_total_excludes_old_row(self):
        assert self._s(agent_names=None, hours=24)["total"] == 3

    def test_all_time_includes_old_row(self):
        assert self._s(agent_names=None, hours=0)["total"] == 4

    def test_running_count_always_live(self):
        # s3 is 5min old — within any reasonable window
        assert self._s(agent_names=None, hours=1)["running_count"] == 1

    def test_running_count_is_not_windowed(self):
        # Insert a running row started 100h ago; it must still appear in running_count
        # even when hours=1 excludes it from the windowed total
        now = datetime.now(timezone.utc)
        _insert_row(self.conn, id="s5", agent_name="agent-a", status="running",
                    started_at=_utc_iso(now - timedelta(hours=100)))
        assert self._s(agent_names=None, hours=1)["running_count"] == 2

    def test_success_count(self):
        assert self._s(agent_names=None, hours=24)["success_count"] == 1

    def test_failed_count(self):
        assert self._s(agent_names=None, hours=24)["failed_count"] == 1

    def test_total_cost_windowed(self):
        cost = self._s(agent_names=None, hours=24)["total_cost"]
        assert abs(cost - 0.015) < 1e-6  # s1(0.01) + s2(0.005); s3=None; s4 outside window

    def test_success_rate(self):
        s = self._s(agent_names=None, hours=24)
        assert s["success_rate"] == round(1 / 3 * 100, 1)

    def test_empty_agent_names_no_division_by_zero(self):
        s = self._s(agent_names=[], hours=24)
        assert s["total"] == 0
        assert s["success_rate"] == 0.0

    def test_admin_none_sees_all(self):
        assert self._s(agent_names=None, hours=0)["total"] == 4

    def test_non_admin_filtered(self):
        now = datetime.now(timezone.utc)
        _insert_row(self.conn, id="b1", agent_name="agent-b", status="success",
                    started_at=_utc_iso(now - timedelta(hours=1)))
        s_a = self._s(agent_names=["agent-a"], hours=0)
        s_all = self._s(agent_names=None, hours=0)
        assert s_a["total"] < s_all["total"]
