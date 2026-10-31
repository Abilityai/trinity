"""#3300 — a broad report listing is complete within `limit` for the set the
caller asked for.

`GET /api/reports` ranked every agent the caller can reach, cut at `limit`, and
the MCP `list_reports` tool then dropped the rows outside the calling agent's
`{self} ∪ permitted`. Since #3810 the backend narrows an agent key to its reach
in SQL, but that reach also carries the agents it spawned, which the tool's
permit set does not — so the tool still post-filtered a page whose cut had been
made over a wider set, and a permitted peer's reports sitting below a window of
a spawned child's could vanish from a short page that looked complete.

The route now takes `agent_names` (the trinity-enterprise#815 shape): repeat the
parameter per name, at most 500, never blank, and it only ever NARROWS what the
caller may see. The MCP sends its permit set, so the cut is made over exactly
the rows the tool delivers.

Runs against a throwaway sqlite carrying the real table, through the real
router function, so the SQL is the code under test.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

CALLER, PEER, KID, STRANGER = "caller", "peer", "kid", "stranger"


@pytest.fixture()
def reports_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-3300.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
    monkeypatch.delenv("DATABASE_URL", raising=False)

    import db.connection as conn_mod

    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.engine import get_engine
    from db.tables import agent_reports, metadata

    metadata.create_all(get_engine(), tables=[agent_reports])
    yield get_engine()


def _seed(agent: str, n: int, start_minute: int) -> None:
    """`n` reports for `agent`, created one minute apart from `start_minute`."""
    from sqlalchemy import update

    from database import db
    from db.engine import get_engine
    from db.tables import agent_reports

    for i in range(n):
        row = db.create_report(
            agent_name=agent, user_id=1, report_type="t", title=f"{agent}-{i}",
            payload={"markdown": "x"}, display_hint="markdown",
        )
        rid = row["id"] if isinstance(row, dict) else row
        with get_engine().begin() as conn:
            conn.execute(update(agent_reports).where(agent_reports.c.id == rid).values(
                created_at=f"2026-10-30T10:{start_minute + i:02d}:00.000000Z"))


def _agent_key():
    from models import User

    return User(id=1, username="owner", email="owner@example.com", role="user",
                agent_name=CALLER, mcp_scope="agent")


def _list(monkeypatch, *, reach, agent_names=None, limit=5, agent=None, user=None):
    """Call the real route. `reach` stands in for `accessible_agent_names`
    (an agent key's #3810 reach, or None for an admin)."""
    from routers import reports

    monkeypatch.setattr(reports, "accessible_agent_names", lambda _u: reach)
    import asyncio

    rows = asyncio.run(reports.list_fleet_reports(
        report_type=None, hours=0, search=None, agent=agent, limit=limit, offset=0,
        agent_names=agent_names, current_user=user or _agent_key(),
    ))
    return [r.agent_name for r in rows]


REACH = [CALLER, KID, PEER]  # self, a spawned child, a permitted peer


def _saturate():
    _seed(PEER, 2, start_minute=0)      # the permitted peer's: oldest
    _seed(CALLER, 1, start_minute=5)
    _seed(KID, 10, start_minute=10)     # a spawned child's: a full window, newest


def test_without_agent_names_the_cut_is_over_the_whole_reach(reports_db, monkeypatch):
    """The failure the tool saw: the page is the child's, so the peer's reports
    and the caller's own lie below the cut."""
    _saturate()
    assert _list(monkeypatch, reach=REACH) == [KID] * 5


def test_agent_names_narrows_before_the_limit(reports_db, monkeypatch):
    """The regression the issue asks for: a permitted peer's reports below a
    saturated window of rows the tool would drop are returned."""
    _saturate()
    got = _list(monkeypatch, reach=REACH, agent_names=[CALLER, PEER])
    assert got == [CALLER, PEER, PEER]


def test_agent_names_never_widens(reports_db, monkeypatch):
    """An agent outside what the caller may see stays out, whatever it asks."""
    _seed(STRANGER, 3, start_minute=0)
    _seed(PEER, 1, start_minute=10)
    assert _list(monkeypatch, reach=REACH, agent_names=[STRANGER, PEER]) == [PEER]
    assert _list(monkeypatch, reach=REACH, agent_names=[STRANGER]) == []


def test_an_admin_is_narrowed_to_exactly_the_names_given(reports_db, monkeypatch):
    from models import User

    _seed(STRANGER, 3, start_minute=0)
    _seed(PEER, 1, start_minute=10)
    admin = User(id=2, username="admin", email="admin@example.com", role="admin")
    assert _list(monkeypatch, reach=None, agent_names=[PEER], user=admin) == [PEER]


def test_agent_names_composes_with_agent(reports_db, monkeypatch):
    _saturate()
    assert _list(monkeypatch, reach=REACH, agent_names=[CALLER, PEER], agent=PEER) == [PEER, PEER]
    assert _list(monkeypatch, reach=REACH, agent_names=[CALLER], agent=PEER) == []


def test_omitted_agent_names_changes_nothing(reports_db, monkeypatch):
    _saturate()
    assert _list(monkeypatch, reach=REACH, limit=50) == _list(
        monkeypatch, reach=REACH, limit=50, agent_names=None)


@pytest.mark.parametrize("bad, why", [
    (["x"] * 501, "at most 500"),
    ([CALLER, ""], "blank"),
    ([CALLER, "   "], "blank"),
])
def test_a_bad_agent_names_is_a_named_422(reports_db, monkeypatch, bad, why):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _list(monkeypatch, reach=REACH, agent_names=bad)
    assert exc.value.status_code == 422
    assert why in exc.value.detail


def test_the_helper_is_shared_and_only_narrows():
    from services.agent_service.helpers import AGENT_NAMES_MAX, narrow_to_names

    assert AGENT_NAMES_MAX == 500
    assert narrow_to_names(None, None) is None
    assert narrow_to_names([CALLER, PEER], None) == [CALLER, PEER]
    assert narrow_to_names(None, [PEER, CALLER]) == [CALLER, PEER]
    assert narrow_to_names([CALLER, PEER], [PEER, STRANGER]) == [PEER]
    assert narrow_to_names([], [PEER]) == []
