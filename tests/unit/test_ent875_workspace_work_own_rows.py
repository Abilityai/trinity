"""The Workspace Work tab shows a share holder only their own turns
(abilityai/trinity-enterprise#875, PR #3777 review point 1).

`/api/enterprise/client-portal/work` reads the executions ledger for every agent
on the viewer's roster, and the roster unions the agents they OWN with the ones
merely SHARED with them. Each card carries `title` — the first 120 characters
of the prompt — so without narrowing, a share holder read every other person's
prompts on a shared agent: the exact leak ent#875 closed on the dashboard.

Driven against a real SQLite through the real accessors, because the fix is a
WHERE clause that must precede each LIMIT. A Workspace turn is stamped with the
viewer's EMAIL (not their id), so the own-rows predicate matches either.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("INTERNAL_API_SECRET", "y" * 32)

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

ME, OWNER = 7, 1
EMAIL = "Mate@Example.com"
SHARED, OWNED = "shared-agent", "owned-agent"


@pytest.fixture
def ops(tmp_path, monkeypatch):
    db_path = tmp_path / "trinity.db"
    conn = sqlite3.connect(str(db_path))
    from sqlalchemy.schema import CreateTable
    from sqlalchemy.dialects import sqlite as sqlite_dialect
    from db.tables import agent_ownership, schedule_executions
    for table in (schedule_executions, agent_ownership):
        conn.execute(str(CreateTable(table).compile(dialect=sqlite_dialect.dialect())))
    conn.commit()
    conn.close()
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from db.schedules import ScheduleOperations
    from db.users import UserOperations
    from db.agents import AgentOperations
    user_ops = UserOperations()
    return ScheduleOperations(user_ops, AgentOperations(user_ops))


def _ts(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def _seed():
    """On the SHARED agent: my 2 turns (one by id, one Workspace turn by email),
    the owner's 20 newer turns, a scheduled run, and a run spawned for me
    (`source_channel_client`). On the OWNED agent: someone else's turn."""
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import schedule_executions
    rows = []

    def add(i, agent, *, minutes, status="success", **who):
        rows.append({
            "id": f"x{i:03d}", "schedule_id": "__manual__", "agent_name": agent,
            "status": status, "started_at": _ts(minutes), "message": f"prompt {i} " + who.pop("text", ""),
            "triggered_by": who.pop("triggered_by", "chat"), **who,
        })

    add(1, SHARED, minutes=500, source_user_id=ME, text="mine-by-id")
    add(2, SHARED, minutes=490, source_user_email="mate@example.com", triggered_by="public", text="mine-by-email")
    add(3, SHARED, minutes=480, source_channel_client="MATE@example.com", triggered_by="agent", text="spawned-for-me")
    add(4, SHARED, minutes=470, triggered_by="schedule", text="owner-cron")
    for i in range(20):
        add(10 + i, SHARED, minutes=100 - i, source_user_id=OWNER, source_user_email="owner@example.com",
            text="owner-secret")
    add(40, SHARED, minutes=1, status="running", source_user_id=OWNER, text="owner-running")
    add(41, SHARED, minutes=2, status="running", source_user_id=ME, text="mine-running")
    add(50, OWNED, minutes=5, source_user_id=99, text="someone-on-my-own-agent")
    keys = ("source_user_id", "source_user_email", "source_channel_client")
    rows = [{k: None for k in keys} | r for r in rows]
    with get_engine().begin() as conn:
        conn.execute(insert(schedule_executions), rows)


def _own(uid=ME, exempt=(OWNED,)):
    return {"own_rows_user_id": uid, "own_rows_email": EMAIL, "own_rows_exempt_agents": list(exempt)}


# ---- the SQL ------------------------------------------------------------------------------------

def test_the_email_counts_as_mine_and_the_narrowing_precedes_the_limit(ops):
    _seed()
    out = ops.get_fleet_executions([SHARED], hours=0, limit=4, **_own())
    assert sorted(r["id"] for r in out) == ["x001", "x002", "x003", "x041"]


def test_an_owned_agent_keeps_every_row(ops):
    _seed()
    out = ops.get_fleet_executions([SHARED, OWNED], hours=0, limit=100, **_own())
    assert "x050" in {r["id"] for r in out}
    assert not any("owner" in (r.get("message") or "") for r in out)


def test_the_window_count_agrees_with_the_list(ops):
    _seed()
    stats = ops.get_fleet_execution_stats([SHARED, OWNED], hours=24, **_own())
    assert stats["total"] == 5 and stats["running_count"] == 1
    unfiltered = ops.get_fleet_execution_stats([SHARED, OWNED], hours=24)
    assert unfiltered["total"] == 27


def test_a_no_rows_id_still_finds_the_email_stamped_rows_and_nothing_else(ops):
    _seed()
    from dependencies import NO_ROWS_USER_ID
    out = ops.get_fleet_executions([SHARED], hours=0, limit=100, **_own(uid=NO_ROWS_USER_ID, exempt=()))
    assert sorted(r["id"] for r in out) == ["x002", "x003"]


# ---- the service, end to end over the real SQL ---------------------------------------------------

@pytest.fixture
def wired(ops, monkeypatch):
    from client_portal.work import service as svc
    ledger = SimpleNamespace(
        get_fleet_executions=ops.get_fleet_executions,
        get_fleet_execution_stats=ops.get_fleet_execution_stats,
        get_running_for_chat=lambda chat_id: [],
        get_user_by_email=lambda email: {"id": ME} if email.lower() == EMAIL.lower() else None,
        get_execution=ops.get_execution,
    )
    monkeypatch.setattr(svc, "core_db", ledger)
    monkeypatch.setattr(svc, "roster_agent_names", lambda email, include_owned: {SHARED, OWNED})
    monkeypatch.setattr(svc.portal_db, "get_owned_roster", lambda email: [{"agent_name": OWNED}])
    monkeypatch.setattr(svc, "_resolve_timeout", lambda agent: 3600)

    async def no_steps(agent, started_at=None, roster=None):
        return svc.WorkSteps(state="none")
    monkeypatch.setattr(svc.pipeline_state, "read_pipeline_steps", no_steps)
    return svc


def test_a_share_holders_work_tab_carries_no_other_persons_prompt(wired):
    _seed()
    out = asyncio.run(wired.get_work(EMAIL, [SHARED, OWNED]))
    titles = [it.title or "" for it in out.now + out.earlier]
    assert titles, "the read returned nothing: the test is blind"
    assert not any("owner" in t for t in titles), titles
    assert {it.id for it in out.now} == {"x041"}
    assert {"x001", "x002", "x003", "x050"} <= {it.id for it in out.earlier}


def test_the_live_activity_lines_follow_the_same_rule(wired, monkeypatch):
    _seed()
    from services import heartbeat_service as hb
    fresh = datetime.now(timezone.utc).timestamp()
    beat = {"tool": "Read", "summary": "notes.md", "ts": fresh}
    beats = {SHARED: {"x040": dict(beat), "x041": dict(beat)}, OWNED: {"x050": dict(beat)}}
    monkeypatch.setattr(hb, "read_execution_activity", lambda name: beats.get(name, {}))
    out = asyncio.run(wired.get_work_activity(EMAIL, [SHARED, OWNED]))
    assert set(out.items) == {"x041", "x050"}
