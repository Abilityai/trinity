"""trinity-enterprise#610 PR A2, §3g L7 — E1: an ask's context, from platform data only.

`GET /api/enterprise/client-portal/asks/{item_id}/context` answers "where did
this ask come from, what ran, what did I say last time" for the person it was
addressed to — a NEW read a portal token can reach, so every field is gated:

* **run** is shown only when the queue row's `execution_id` (AGENT-written,
  via `context.execution_id`) names a run that (a) belongs to the ask's agent,
  (b) was LIVE when the ask was filed — `started_at <= created_at <=
  (completed_at or now) + grace`, compared as parsed timestamps (Invariant #16)
  — and (c) was a schedule or manual trigger, or the viewer owns its source.
  Otherwise `run is None`: an agent that names another client's run must not
  disclose it. The run's label names the schedule for a PLATFORM principal only.
* **origin** carries an excerpt (the 3 messages before the ask) only when the
  run's portal session is verified as the viewer's; otherwise it is the ask's
  own chat (Main, for every ingested ask) with no excerpt.
* **recent_answers** are the viewer's own answered asks from this agent.
* No `cost`, no `execution_id` anywhere in the JSON (§6.9 — stricter than Work).
* 404 is uniform for missing / not-mine / off-roster / invisible kind
  (Invariant #8); an unreadable roster is 503, never an empty context.

Harness: a real SQLite built by `init_schema` (the ent#428/#3059 one).
"""
from __future__ import annotations

import json
import uuid

import pytest

pytestmark = pytest.mark.unit

AGENT = "billing"
OTHER_AGENT = "scout"


@pytest.fixture()
def ctx_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-ent610-ctx.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))
    import sqlite3
    from db.schema import init_schema
    raw = sqlite3.connect(db_file)
    init_schema(raw.cursor(), raw)
    raw.commit()
    raw.close()
    from db.engine import get_engine
    yield get_engine()


@pytest.fixture()
def email():
    # The engine is cached at first use — a unique viewer per test keeps rows apart.
    return f"client-{uuid.uuid4().hex[:8]}@example.com"


@pytest.fixture(autouse=True)
def roster(monkeypatch):
    state = {"off": set(), "down": False}
    import client_portal.service as portal_service

    def _on_roster(agent_name, email, include_owned=False):
        if state["down"]:
            raise RuntimeError("roster unreadable")
        return agent_name not in state["off"]

    monkeypatch.setattr(portal_service, "agent_on_roster", _on_roster)
    return state


def _exec(engine, *, agent=AGENT, triggered_by="schedule", started="2026-09-29T09:00:00.000000Z",
          completed="2026-09-29T09:20:00.000000Z", source_user_email=None, source_channel=None,
          chat=None, schedule_id="sched-1", cost=0.42):
    from sqlalchemy import text
    eid = f"exec-{uuid.uuid4().hex[:10]}"
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO schedule_executions (id, schedule_id, agent_name, status, started_at, "
            " completed_at, message, triggered_by, source_user_email, source_channel, "
            " source_channel_chat_id, cost) "
            "VALUES (:id, :sid, :a, 'success', :s, :c, 'Run the nightly billing', :t, :u, :ch, :chat, :cost)"
        ), {"id": eid, "sid": schedule_id, "a": agent, "s": started, "c": completed, "t": triggered_by,
            "u": source_user_email, "ch": source_channel, "chat": chat, "cost": cost})
    return eid


def _schedule(engine, sid="sched-1", name="nightly-billing", agent=AGENT):
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO agent_schedules (id, agent_name, name, cron_expression, message, enabled, "
            " timezone, owner_id, created_at, updated_at) "
            "VALUES (:id, :a, :n, '0 9 * * *', 'm', 1, 'UTC', 1, :t, :t)"
        ), {"id": sid, "a": agent, "n": name, "t": "2026-09-01T00:00:00.000000Z"})


def _session(engine, sid, email, *, agent=AGENT, is_main=0, title=None):
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO enterprise_portal_sessions "
            "(id, agent_name, client_email, title, created_at, last_message_at, message_count, is_main) "
            "VALUES (:id, :a, :e, :t, :c, :c, 0, :m)"
        ), {"id": sid, "a": agent, "e": email.lower(), "t": title, "c": "2026-09-01T00:00:00.000000Z",
            "m": is_main})


def _msg(engine, *, session_id, email, at, content, role="assistant", agent=AGENT, cost=None):
    from sqlalchemy import text
    mid = f"m-{uuid.uuid4().hex[:8]}"
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO enterprise_portal_messages "
            "(id, agent_name, client_email, session_id, role, content, cost, created_at) "
            "VALUES (:id, :a, :e, :s, :r, :c, :cost, :at)"
        ), {"id": mid, "a": agent, "e": email.lower(), "s": session_id, "r": role, "c": content,
            "cost": cost, "at": at})
    return mid


def _ask(email, *, agent=AGENT, created="2026-09-29T09:10:00.000000Z", execution_id=None,
         chat_id=None, kind="approval", title="Pay the vendor?"):
    from database import db
    from services.operator_queue_service import _clamp_ingested_item
    item = _clamp_ingested_item({
        "id": f"req-{uuid.uuid4().hex[:12]}", "type": kind, "title": title,
        "question": "Release it?", "options": ["yes", "no"], "addressed_to_email": email,
    }, agent)
    # Platform-written, as ingestion writes them (ent#429 stamps the chat; the
    # agent's own `context.execution_id` becomes the column).
    ctx = {}
    if execution_id:
        ctx["execution_id"] = execution_id
    if chat_id:
        ctx["workspace_session_id"] = chat_id
    item["context"] = ctx or None
    item["created_at"] = created
    return db.create_operator_queue_item(agent, item)


def _ctx(item_id, email, is_platform=False):
    from client_portal.asks import service
    return service.get_ask_context(item_id, email, is_platform)


# --- origin --------------------------------------------------------------------

def test_origin_is_the_three_messages_before_the_ask_from_a_verified_session(ctx_db, email):
    _session(ctx_db, "s-run", email, title="September close")
    _session(ctx_db, "s-main", email, is_main=1)
    at = lambda m: f"2026-09-29T09:{m:02d}:00.000000Z"  # noqa: E731
    _msg(ctx_db, session_id="s-run", email=email, at=at(1), content="first", role="user")
    m2 = _msg(ctx_db, session_id="s-run", email=email, at=at(2), content="**Invoice** found", cost=0.9)
    m3 = _msg(ctx_db, session_id="s-run", email=email, at=at(3), content="checking the PO", role="user")
    m4 = _msg(ctx_db, session_id="s-run", email=email, at=at(4), content="It matches.")
    _msg(ctx_db, session_id="s-run", email=email, at=at(12), content="after the ask")
    run = _exec(ctx_db, triggered_by="public", source_user_email=email, source_channel="portal",
                chat="s-run", started=at(0), completed=at(15))
    ask = _ask(email, execution_id=run, chat_id="s-main", created=at(10))

    ctx = _ctx(ask, email)
    assert ctx.origin is not None and ctx.origin.verified is True
    assert ctx.origin.chat_id == "s-run" and ctx.origin.title == "September close"
    assert [m.id for m in ctx.origin.messages] == [m2, m3, m4]
    assert ctx.origin.messages[0].excerpt == "Invoice found"   # markdown stripped
    assert ctx.run is not None and ctx.run.kind == "turn"


def test_an_unverified_session_falls_back_to_the_asks_own_chat_with_no_excerpt(ctx_db, email):
    other = f"other-{email}"
    _session(ctx_db, "s-theirs", other)
    _session(ctx_db, "s-main", email, is_main=1)
    _msg(ctx_db, session_id="s-theirs", email=other, at="2026-09-29T09:05:00.000000Z", content="secret")
    # A schedule run (valid for the viewer) whose chat stamp is someone else's thread.
    run = _exec(ctx_db, source_channel="portal", chat="s-theirs")
    ask = _ask(email, execution_id=run, chat_id="s-main")
    ctx = _ctx(ask, email)
    assert ctx.origin is not None
    assert ctx.origin.verified is False and ctx.origin.chat_id == "s-main" and ctx.origin.is_main is True
    assert ctx.origin.messages == []
    assert "secret" not in ctx.model_dump_json()


def test_no_chat_at_all_is_no_origin(ctx_db, email):
    ask = _ask(email)
    assert _ctx(ask, email).origin is None


# --- run -----------------------------------------------------------------------

def test_a_schedule_run_live_when_the_ask_was_filed_is_shown(ctx_db, email):
    _schedule(ctx_db)
    ask = _ask(email, execution_id=_exec(ctx_db))
    ctx = _ctx(ask, email)
    assert ctx.run is not None and ctx.run.kind == "schedule"
    assert ctx.run.started_at.startswith("2026-09-29T09:00:00")
    # T12: an external client never sees the schedule's name.
    assert ctx.run.label == "Asked during a scheduled run"


def test_the_run_label_names_the_schedule_for_a_platform_principal_only(ctx_db, email):
    _schedule(ctx_db)
    ask = _ask(email, execution_id=_exec(ctx_db))
    assert _ctx(ask, email, is_platform=True).run.label == "Asked by the nightly-billing run"
    assert "nightly-billing" not in _ctx(ask, email, is_platform=False).model_dump_json()


def test_another_users_run_on_the_same_agent_is_never_shown(ctx_db, email):
    """The agent writes `execution_id`: naming another client's turn on the same
    agent must not disclose it — not its kind, not its time."""
    run = _exec(ctx_db, triggered_by="public", source_user_email=f"other-{email}",
                source_channel="portal", chat="s-x")
    ask = _ask(email, execution_id=run)
    assert _ctx(ask, email).run is None


def test_a_run_outside_its_live_window_is_not_shown(ctx_db, email):
    early = _exec(ctx_db, started="2026-09-29T07:00:00.000000Z", completed="2026-09-29T07:05:00.000000Z")
    assert _ctx(_ask(email, execution_id=early), email).run is None
    late = _exec(ctx_db, started="2026-09-29T09:30:00.000000Z", completed="2026-09-29T09:40:00.000000Z")
    assert _ctx(_ask(email, execution_id=late), email).run is None
    # Inside the grace after completion (the poller ingests a few seconds late): shown.
    graced = _exec(ctx_db, started="2026-09-29T09:00:00.000000Z", completed="2026-09-29T09:09:00.000000Z")
    assert _ctx(_ask(email, execution_id=graced), email).run is not None


def test_a_run_of_another_agent_is_not_shown(ctx_db, email):
    run = _exec(ctx_db, agent=OTHER_AGENT)
    assert _ctx(_ask(email, execution_id=run), email).run is None


def test_a_missing_execution_is_no_run(ctx_db, email):
    assert _ctx(_ask(email, execution_id="exec-does-not-exist"), email).run is None


# --- recent answers -----------------------------------------------------------

def test_recent_answers_are_the_viewers_own_from_this_agent_newest_first(ctx_db, email):
    from client_portal.asks import service
    mine = []
    for i in range(4):
        a = _ask(email, title=f"Earlier {i}", created=f"2026-09-2{i}T10:00:00.000000Z")
        service.answer_ask(a, email, is_platform=False, response="yes", response_text=None)
        mine.append(a)
    other_agent = _ask(email, agent=OTHER_AGENT, title="elsewhere")
    service.answer_ask(other_agent, email, is_platform=False, response="yes", response_text=None)
    _ask(email, title="still pending")
    this = _ask(email, title="This one")
    ctx = _ctx(this, email)
    assert [r.id for r in ctx.recent_answers] == list(reversed(mine))[:3]
    assert all(r.answer == "yes" for r in ctx.recent_answers)
    assert this not in [r.id for r in ctx.recent_answers]


# --- the route: door, uniform 404, 503, no cost / execution_id -----------------

def _client(email, is_platform=False, calls=None):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from client_portal.asks.router import router
    from client_portal.portal_auth import PortalPrincipal, get_portal_principal
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_portal_principal] = lambda: PortalPrincipal(email=email, is_platform=is_platform)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def limiter(monkeypatch):
    calls = []
    from services import rate_limiter
    monkeypatch.setattr(rate_limiter, "enforce", lambda key, limit, window, **kw: calls.append((key, limit, window)))
    return calls


def test_the_route_carries_no_cost_and_no_execution_id(ctx_db, email, limiter):
    _session(ctx_db, "s-run", email)
    _msg(ctx_db, session_id="s-run", email=email, at="2026-09-29T09:05:00.000000Z", content="hello", cost=1.5)
    run = _exec(ctx_db, triggered_by="public", source_user_email=email, source_channel="portal", chat="s-run")
    ask = _ask(email, execution_id=run, chat_id="s-run")
    r = _client(email).get(f"/api/enterprise/client-portal/asks/{ask}/context")
    assert r.status_code == 200, r.text
    body = r.text
    assert "cost" not in body and "execution_id" not in body and run not in body
    assert json.loads(body)["origin"]["messages"][0]["excerpt"] == "hello"
    assert limiter == [(f"portal_ask_context:{email}", 120, 60)]


def test_404_is_one_body_for_missing_not_mine_off_roster_and_invisible_kind(ctx_db, email, roster):
    theirs = _ask(f"other-{email}")
    off = _ask(email, agent="gone-agent")
    roster["off"].add("gone-agent")
    hidden = _ask(email, kind="skill_not_found")
    c = _client(email)
    bodies = {c.get(f"/api/enterprise/client-portal/asks/{i}/context").text
              for i in ("no-such-id", theirs, off, hidden)}
    codes = {c.get(f"/api/enterprise/client-portal/asks/{i}/context").status_code
             for i in ("no-such-id", theirs, off, hidden)}
    assert codes == {404} and len(bodies) == 1


def test_an_unreadable_roster_is_503_never_an_empty_context(ctx_db, email, roster):
    ask = _ask(email)
    roster["down"] = True
    r = _client(email).get(f"/api/enterprise/client-portal/asks/{ask}/context")
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "asks_unavailable"


def test_the_answer_path_still_404s_on_a_roster_outage(ctx_db, email, roster):
    """`_owned_ask` is shared; the ANSWER path keeps its non-strict roster (its
    refusal stays the uniform 404 whatever the cause)."""
    from client_portal.asks import service
    ask = _ask(email)
    roster["down"] = True
    with pytest.raises(service.AskError) as e:
        service.answer_ask(ask, email, False, "yes", None)
    assert e.value.status_code == 404


def test_the_asks_router_declares_its_mcp_surface():
    from pathlib import Path
    import client_portal.asks.router as r
    first = Path(r.__file__).read_text().splitlines()[0]
    assert first.startswith("# mcp: none — "), first
