"""Per-agent report filters — search + time window (#1539).

The fleet list has had `report_type`/`hours`/`search` since #918; the per-agent
route had `report_type` only, which made the Agent Detail Reports tab a flat
unfilterable list and silently dropped both filters for any caller that scoped to
one agent (including the #1538 `list_reports` MCP tool).

Locked here:
  * the per-agent list honours `hours` and `search`
  * `search` on a single-agent list does NOT match on `agent_name` — every row
    carries it, so a term matching the agent's own name would return the whole
    history and look like search was ignored
  * the `database.py` facade forwards the new keywords. This is the regression
    that actually bit: the ops signature grew two parameters while the facade
    still delegated positionally, so `limit` bound to `hours` and every call
    500'd with `unexpected keyword argument 'hours'`. A test that mocks `db`
    wholesale cannot see it (learnings 2026-07-04, the facade-delegation pitfall).
"""
from __future__ import annotations

import inspect
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
    """Create `agent_reports` from the SAME metadata the code uses.

    This used to be a hand-copied `CREATE TABLE`, i.e. a second declaration of a
    table that already has one — and it broke the moment ent#365 added the
    audience columns, with an error about the FIXTURE rather than about the
    filters this suite exists to test. Deriving it means the next column arrives
    here for free, and a filter test can never pass against a table shape the
    product does not have.
    """
    from sqlalchemy.schema import CreateTable
    from sqlalchemy.dialects import sqlite as sqlite_dialect
    from db.tables import agent_reports

    conn.execute(str(CreateTable(agent_reports).compile(dialect=sqlite_dialect.dialect())))
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


def _seed(ops_obj, agent: str, report_type: str, title: str) -> str:
    row = ops_obj.create_report(
        agent_name=agent,
        user_id=1,
        report_type=report_type,
        title=title,
        payload={"markdown": title},
        display_hint="markdown",
    )
    return row["id"] if isinstance(row, dict) else row


def _backdate(report_id: str, days: int) -> None:
    from sqlalchemy import update
    from db.engine import get_engine
    from db.tables import agent_reports

    when = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    with get_engine().begin() as conn:
        conn.execute(
            update(agent_reports).where(agent_reports.c.id == report_id).values(created_at=when)
        )


def test_search_matches_title_and_type(ops):
    _seed(ops, "scout", "recon.weekly_summary", "Week 30 recon: 14 leads")
    _seed(ops, "scout", "ops.daily_health", "Daily health: all green")

    by_title = ops.get_reports_for_agent("scout", search="Week 30")
    assert [r["title"] for r in by_title] == ["Week 30 recon: 14 leads"]

    by_type = ops.get_reports_for_agent("scout", search="ops.daily")
    assert [r["title"] for r in by_type] == ["Daily health: all green"]


def test_search_does_not_match_the_agents_own_name(ops):
    """Every row on a single-agent list carries that agent_name, so including it
    in the search OR would return the whole history for a term that happens to
    match the agent — indistinguishable from search being ignored."""
    _seed(ops, "recon-bot", "ops.daily_health", "Daily health: all green")
    _seed(ops, "recon-bot", "ops.daily_health", "Daily health: one warning")

    assert ops.get_reports_for_agent("recon-bot", search="recon") == []
    # …while the fleet list still finds an agent by name, which is how you ask
    # "everything recon-bot published".
    fleet = ops.get_fleet_reports(None, search="recon")
    assert len(fleet) == 2


def test_hours_window_excludes_older_rows(ops):
    fresh = _seed(ops, "scout", "ops.daily_health", "today")
    old = _seed(ops, "scout", "ops.daily_health", "last month")
    _backdate(old, days=40)

    assert [r["title"] for r in ops.get_reports_for_agent("scout", hours=168)] == ["today"]
    both = ops.get_reports_for_agent("scout", hours=None)
    assert {r["title"] for r in both} == {"today", "last month"}
    assert fresh  # sanity: the fixture actually created rows


def test_filters_compose(ops):
    _seed(ops, "scout", "recon.weekly_summary", "Week 30 recon")
    _seed(ops, "scout", "ops.daily_health", "Week 30 health")

    rows = ops.get_reports_for_agent(
        "scout", report_type="recon.weekly_summary", search="Week 30", hours=168
    )
    assert [r["title"] for r in rows] == ["Week 30 recon"]


def test_facade_forwards_the_new_filters():
    """The bug this file exists for: the facade delegated positionally, so two
    new ops parameters rebound `limit`→`hours` and every request 500'd. Assert
    the facade accepts them AND hands them through by keyword."""
    try:
        import database as database_module
    except ImportError:  # pragma: no cover - backend venv required
        pytest.skip("backend venv required")

    sig = inspect.signature(database_module.DatabaseManager.get_reports_for_agent)
    assert {"hours", "search"} <= set(sig.parameters), (
        "facade signature lost the #1539 filters"
    )

    captured = {}

    class _Ops:
        def get_reports_for_agent(self, agent_name, **kwargs):
            captured.update(kwargs)
            captured["agent_name"] = agent_name
            return []

    facade = object.__new__(database_module.DatabaseManager)
    facade._report_ops = _Ops()

    facade.get_reports_for_agent("scout", report_type="t", hours=24, search="q", limit=7, offset=2)

    # Keyword forwarding is the point: positional delegation is what broke, so
    # asserting the VALUES land on the right names is the regression guard.
    assert captured == {
        "agent_name": "scout",
        "report_type": "t",
        "hours": 24,
        "search": "q",
        "limit": 7,
        "offset": 2,
    }
