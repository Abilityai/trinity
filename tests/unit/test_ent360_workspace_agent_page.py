"""The Workspace agent page (ent#360).

The page is a read surface, so almost nothing here is about what it shows. It is
about what it must NOT show, because two constraints collide on one payload:

  * **AC #7** — it reports, it does not configure. No costs, no model, no logs.
  * **The viewer may be an external client**, not an operator. The same page
    serves a portal-token client and a platform user.

Both are enforced by *projection* in the service rather than by filtering in the
template, so the tests assert on absence: a field that never leaves the service
cannot be surfaced by a later UI edit. That is the whole reason these are worth
writing — a page that renders correctly today and leaks `cost` after someone
adds a column to a list view is the failure mode.

The other half is the cross-agent report read: report ids are global, so without
an agent check any rostered agent's page would read any report in the install.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

# ent#365: the report read is scoped to who is asking, so every call names a
# reader. These suites keep their own subjects (agent isolation, row windowing);
# the audience rule itself is pinned in test_ent365_report_audience.py.
CLIENT_EMAIL = "client@example.com"

AGENT = "scribe"
OTHER = "recon"
EMAIL = "alice@example.com"


# ---------------------------------------------------------------------------
# What must never reach a Workspace viewer
# ---------------------------------------------------------------------------

def test_recent_work_carries_no_message_cost_or_model(monkeypatch):
    """The underlying accessor returns all three. `message` is another user's
    prompt; `cost` and `model_used` are excluded by AC #7.

    Asserted as an EXACT dict on purpose: an allow-list of absences would pass
    the day someone adds a column to the accessor. #2161 added exactly one field
    to this shape (`schedule_name`), and this is where that has to be argued.
    """
    from client_portal import agent_page

    monkeypatch.setattr(agent_page.db, "get_agent_executions_summary", lambda *a, **k: [{
        "id": "e1", "status": "success", "triggered_by": "schedule",
        "started_at": "2026-08-13T10:00:00Z", "completed_at": "2026-08-13T10:00:09Z",
        "duration_ms": 9000,
        # Everything below must be projected away.
        "message": "reconcile the Q3 invoices for ACME",
        "cost": 0.42, "model_used": "claude-opus-5",
        "source_user_email": "someone.else@example.com",
        "claude_session_id": "uuid-1234",
    }])

    row = agent_page._recent_work(AGENT)[0]

    assert row == {
        "id": "e1", "status": "success", "triggered_by": "schedule",
        "started_at": "2026-08-13T10:00:00Z", "completed_at": "2026-08-13T10:00:09Z",
        "duration_ms": 9000, "schedule_name": None,
    }


def test_a_report_belonging_to_another_agent_is_not_readable(monkeypatch):
    """Report ids are global. The roster gate only proves the caller may reach
    THIS agent, so without the ownership check its page becomes a reader for
    every report in the install."""
    from client_portal import agent_page

    monkeypatch.setattr(agent_page.db, "get_report_for_client", lambda rid, _email: {
        "id": rid, "agent_name": OTHER, "title": "someone else's numbers",
        "payload": {"secret": 1},
    })

    assert agent_page.report_detail(AGENT, "r1", client_email=CLIENT_EMAIL) is None


def test_a_missing_report_and_a_foreign_one_are_indistinguishable(monkeypatch):
    """Both return None → the same 404. A different answer would let a caller
    test whether a report id exists (invariant #8)."""
    from client_portal import agent_page

    monkeypatch.setattr(agent_page.db, "get_report_for_client", lambda rid, _email: None)
    missing = agent_page.report_detail(AGENT, "nope", client_email=CLIENT_EMAIL)

    monkeypatch.setattr(agent_page.db, "get_report_for_client",
                        lambda rid, _email: {"id": rid, "agent_name": OTHER, "payload": {}})
    foreign = agent_page.report_detail(AGENT, "r1", client_email=CLIENT_EMAIL)

    assert missing is None and foreign is None


def test_the_agents_own_report_is_returned(monkeypatch):
    from client_portal import agent_page

    monkeypatch.setattr(agent_page.db, "get_report_for_client", lambda rid, _email: {
        "id": rid, "agent_name": AGENT, "title": "Weekly", "payload": {"rows": []},
        "report_type": "recon.weekly", "display_hint": "table",
        "period_start": None, "period_end": None, "created_at": "2026-08-13T00:00:00Z",
    })

    got = agent_page.report_detail(AGENT, "r1", client_email=CLIENT_EMAIL)

    assert got["id"] == "r1" and got["payload"] == {"rows": []}


# ---------------------------------------------------------------------------
# Degradation — AC #6: renders for a stopped agent, degraded not empty
# ---------------------------------------------------------------------------

def test_health_is_unknown_rather_than_unhealthy_when_never_checked(monkeypatch):
    """Monitoring is default-OFF (#1121), so on many installs no agent has ever
    been checked. Rendering "unhealthy" there would be a lie about every agent
    on the instance."""
    from client_portal import agent_page

    monkeypatch.setattr(agent_page.db, "get_latest_health_check", lambda *a, **k: None)

    assert agent_page._health(AGENT) == {"status": "unknown", "checked_at": None}


def test_a_failing_data_source_degrades_that_section_only(monkeypatch):
    """A page that 500s because the operator queue is unhappy is worse than one
    that renders without its asks section."""
    from client_portal import agent_page

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(agent_page.db, "list_operator_queue_items", boom)
    monkeypatch.setattr(agent_page.db, "get_agent_executions_summary", boom)
    monkeypatch.setattr(agent_page.db, "get_latest_health_check", boom)
    monkeypatch.setattr(agent_page.db, "get_agent_analytics", boom)

    page = agent_page.build_page(EMAIL, AGENT, {"description": "d"}, window="7d")

    # #2449: `asks` left this payload — the page renders them from `/asks`
    # through `PortalAsks`, one projection instead of two.
    assert "asks" not in page
    assert page["recent_work"] == []
    assert page["header"]["health"]["status"] == "unknown"
    assert page["stats"]["unavailable"] is True
    assert page["header"]["description"] == "d"   # what IS known still renders


def test_capabilities_come_from_the_roster_briefing(monkeypatch):
    """"What it can do" is a projection of the briefing the roster already
    carries (#138/ent#380), not a second mechanism — ent#178 is the unified
    config it becomes a view of."""
    from client_portal import agent_page

    monkeypatch.setattr(agent_page.db, "get_latest_health_check", lambda *a, **k: None)
    monkeypatch.setattr(agent_page.db, "get_agent_executions_summary", lambda *a, **k: [])
    monkeypatch.setattr(agent_page.db, "list_operator_queue_items", lambda **k: [])
    monkeypatch.setattr(agent_page.db, "get_agent_analytics", lambda *a, **k: {})
    monkeypatch.setattr(agent_page.portal_db, "first_try_stats",
                        lambda *a, **k: {"terminal": 0, "first_try": 0, "rate": None})

    card = {"playbooks": [{"title": "Weekly report", "starter_prompt": "run it"}]}
    page = agent_page.build_page(EMAIL, AGENT, card, window="7d")

    assert page["capabilities"] == card["playbooks"]


# ---------------------------------------------------------------------------
# first-try rate — the AC #3 metric that DOES have a source
# ---------------------------------------------------------------------------

@pytest.fixture()
def exec_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-agent-page.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))
    from db.engine import get_engine
    from db.tables import metadata as oss_metadata, schedule_executions
    oss_metadata.create_all(get_engine(), tables=[schedule_executions])
    yield get_engine()


def _exec(engine, *, eid, status, retry_count, at="2099-01-01T00:00:00Z", agent=AGENT):
    from db.tables import schedule_executions as se
    with engine.begin() as conn:
        conn.execute(se.insert().values(
            id=eid, schedule_id="__manual__", agent_name=agent, status=status,
            started_at=at, message="m", triggered_by="manual", retry_count=retry_count,
        ))


def test_first_try_excludes_a_success_that_needed_a_retry(exec_db):
    """Distinct from the success rate the analytics accessor reports, which
    counts a retried-then-succeeded execution as a success — the right answer to
    "does it get there in the end", the wrong one to "does it get there first
    time"."""
    from client_portal import db as pdb

    _exec(exec_db, eid="a", status="success", retry_count=0)
    _exec(exec_db, eid="b", status="success", retry_count=2)
    _exec(exec_db, eid="c", status="failed", retry_count=0)

    got = pdb.first_try_stats(AGENT, 720)

    assert got["terminal"] == 3
    assert got["first_try"] == 1


def test_a_pre_retry_column_row_counts_as_first_try(exec_db):
    """`retry_count` is NULL on rows written before #678 — such a success
    genuinely had no retry, so NULL reads as zero rather than excluding it."""
    from client_portal import db as pdb

    _exec(exec_db, eid="a", status="success", retry_count=None)

    assert pdb.first_try_stats(AGENT, 720)["first_try"] == 1


def test_another_agents_executions_are_not_counted(exec_db):
    from client_portal import db as pdb

    _exec(exec_db, eid="a", status="success", retry_count=0, agent=OTHER)

    assert pdb.first_try_stats(AGENT, 720)["terminal"] == 0


def test_no_terminal_executions_reports_no_rate_rather_than_zero(exec_db):
    """0% would read as "it fails every time"; a fresh agent simply has no
    first-try rate yet."""
    from client_portal import db as pdb

    assert pdb.first_try_stats(AGENT, 720)["rate"] is None


def test_running_and_queued_rows_are_not_terminal(exec_db):
    from client_portal import db as pdb

    _exec(exec_db, eid="a", status="running", retry_count=0)
    _exec(exec_db, eid="b", status="queued", retry_count=0)

    assert pdb.first_try_stats(AGENT, 720)["terminal"] == 0
