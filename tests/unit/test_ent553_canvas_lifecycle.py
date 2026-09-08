"""ent#553 — canvases can be removed, pinned, and are bounded.

An agent that uses its canvas the way ent#438 intends accumulates one per
report, per topic, per run. Before this the Workspace could only ever ADD to
that pile: no delete on the client-portal surface at all, no ordering beyond
"newest updated", and nothing bounding the table.

The three decisions this file pins, because each had a plausible alternative:

**The cap refuses, it never evicts.** `agent_canvases` is bounded per canvas by
its composite key, but `canvas_id` is agent-chosen, so the COUNT is unbounded —
ent#438 read the first fact and concluded no retention was needed; the second
axis was missed, not decided. The bound is a per-agent cap with a named refusal
rather than a retention window, by operator ruling: a window deletes a person's
surfaces on a timer, which is the #1638 failure direction, while a cap destroys
nothing and tells the agent to retire one itself.

**Updating is never refused.** The cap is checked only on the INSERT branch, so
an agent at its limit can still keep its live surfaces current. A cap that
froze updates would punish exactly the well-behaved agent that reuses ids.

**Deleting is owner-or-admin, and the pin is a human's.** This mirrors the
answer ent#548 gives for files. A canvas is one shared surface with no per-user
copy, so a non-owner has no "hide it from my list" middle ground to be offered
— they get no control at all (AC #2). The agent keeps deleting its own
(`clear_canvas`), and `pinned` is deliberately absent from every agent-facing
surface: an agent that could pin itself to the top would defeat the ordering
the pin exists to give the person.
"""
from __future__ import annotations

import pytest

from models import CANVAS_MAX_PER_AGENT, User


@pytest.fixture()
def canvas_db(tmp_path, monkeypatch):
    """Fresh sqlite carrying the tables the canvas layer reads."""
    db_file = tmp_path / "trinity-canvas-553.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))

    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.engine import get_engine
    from db.tables import (
        metadata as m, agent_canvases, agent_ownership, users, schedule_executions,
    )
    m.create_all(get_engine(),
                 tables=[agent_canvases, agent_ownership, users, schedule_executions])

    from sqlalchemy import insert
    with get_engine().begin() as conn:
        conn.execute(insert(users).values(id=1, username="alice", role="user",
                                          email="alice@example.com",
                                          created_at="t", updated_at="t"))
        conn.execute(insert(agent_ownership).values(
            agent_name="agent-a", owner_id=1, created_at="t"))
    yield str(db_file)


def _write(agent: str, canvas_id: str, **kw):
    from database import db
    return db.upsert_agent_canvas(agent, canvas_id, blocks=kw.pop("blocks", []), **kw)


# --- the cap ----------------------------------------------------------------

def test_the_cap_refuses_a_new_canvas_by_name(canvas_db, monkeypatch):
    """The refusal names the count, the limit, and the way out. An agent reads
    this string — "you are full" with no remedy is a dead end."""
    import db.canvas as canvas_mod
    from db.canvas import CanvasLimitExceeded
    monkeypatch.setattr(canvas_mod, "CANVAS_MAX_PER_AGENT", 3)

    for i in range(3):
        _write("agent-a", f"c{i}")

    with pytest.raises(CanvasLimitExceeded) as excinfo:
        _write("agent-a", "c3")

    message = str(excinfo.value)
    assert "3" in message and "retire" in message.lower()


def test_updating_an_existing_canvas_is_never_refused_at_the_cap(canvas_db, monkeypatch):
    """The property that makes the cap safe to ship. An agent at its limit must
    still be able to keep its live surfaces current; a cap that blocked updates
    would freeze the fleet's dashboards the moment it bit."""
    import db.canvas as canvas_mod
    monkeypatch.setattr(canvas_mod, "CANVAS_MAX_PER_AGENT", 2)

    _write("agent-a", "c0", title="first")
    _write("agent-a", "c1", title="second")

    updated = _write("agent-a", "c0", title="rewritten")
    assert updated["title"] == "rewritten"


def test_the_cap_is_per_agent_not_global(canvas_db, monkeypatch):
    """One busy agent must not stop another from creating anything."""
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import agent_ownership
    import db.canvas as canvas_mod
    monkeypatch.setattr(canvas_mod, "CANVAS_MAX_PER_AGENT", 2)

    with get_engine().begin() as conn:
        conn.execute(insert(agent_ownership).values(
            agent_name="agent-b", owner_id=1, created_at="t"))

    _write("agent-a", "c0")
    _write("agent-a", "c1")
    assert _write("agent-b", "c0")["canvas_id"] == "c0"


def test_the_default_cap_is_generous_enough_to_be_a_runaway_guard(canvas_db):
    """A bound nobody should feel in normal use. Pinned as a floor, not a
    value: raising it is always safe, lowering it starts refusing real work."""
    assert CANVAS_MAX_PER_AGENT >= 50


# --- the pin ----------------------------------------------------------------

def test_pinned_canvases_sort_first(canvas_db):
    from database import db
    _write("agent-a", "old")
    _write("agent-a", "new")
    db.set_agent_canvas_pinned("agent-a", "old", True)

    order = [c["canvas_id"] for c in db.list_agent_canvases("agent-a")]
    assert order[0] == "old", "a pin must outrank recency, or it does nothing"


def test_a_pin_survives_the_agent_rewriting_its_canvas(canvas_db):
    """The pin is the READER's ordering. An agent updating its canvas must not
    silently unpin it — that would make the pin last only until the next run,
    which for a canvas in daily use is no time at all."""
    from database import db
    _write("agent-a", "c0", title="v1")
    db.set_agent_canvas_pinned("agent-a", "c0", True)

    after = _write("agent-a", "c0", title="v2")
    assert after["pinned"] is True
    assert db.list_agent_canvases("agent-a")[0]["pinned"] is True


def test_a_new_canvas_is_never_born_pinned(canvas_db):
    assert _write("agent-a", "c0")["pinned"] is False


def test_pinning_an_absent_canvas_reports_that_it_did_nothing(canvas_db):
    """The route turns this into a 404 rather than a silent success."""
    from database import db
    assert db.set_agent_canvas_pinned("agent-a", "ghost", True) is False


# --- bulk delete ------------------------------------------------------------

def test_bulk_delete_returns_only_the_ids_that_existed(canvas_db):
    """"3 of 5 removed" has to be sayable. A rowcount cannot name WHICH, and a
    bulk action that misreports its own scope is worse than one that does less."""
    from database import db
    _write("agent-a", "c0")
    _write("agent-a", "c1")

    deleted = db.delete_agent_canvases("agent-a", ["c0", "c1", "never-existed"])
    assert sorted(deleted) == ["c0", "c1"]
    assert db.count_agent_canvases("agent-a") == 0


def test_bulk_delete_cannot_reach_another_agents_canvases(canvas_db):
    """`agent_name` is in the WHERE, so an authorized caller for one agent
    cannot delete another's rows by passing foreign ids."""
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import agent_ownership
    from database import db

    with get_engine().begin() as conn:
        conn.execute(insert(agent_ownership).values(
            agent_name="agent-b", owner_id=1, created_at="t"))
    _write("agent-b", "secret")

    assert db.delete_agent_canvases("agent-a", ["secret"]) == []
    assert db.count_agent_canvases("agent-b") == 1


def test_bulk_delete_of_nothing_is_a_no_op(canvas_db):
    from database import db
    assert db.delete_agent_canvases("agent-a", []) == []


# --- who may remove ---------------------------------------------------------

def _user(username="alice", role="user", agent_name=None):
    return User(id=1, username=username, role=role, agent_name=agent_name)


def test_an_agent_key_may_only_remove_its_own_canvas(monkeypatch):
    from fastapi import HTTPException
    from routers import canvas as canvas_router

    canvas_router._gate_human_removal(_user(agent_name="agent-a"), "agent-a")

    with pytest.raises(HTTPException) as excinfo:
        canvas_router._gate_human_removal(_user(agent_name="agent-a"), "agent-b")
    assert excinfo.value.status_code == 403


def test_a_human_must_own_the_agent_to_remove_its_canvas(monkeypatch):
    """The narrowing. Before ent#553 any user the agent was merely SHARED with
    could delete through this route; no UI called it, so nothing depended on
    the wider gate."""
    from fastapi import HTTPException
    from routers import canvas as canvas_router
    import dependencies

    monkeypatch.setattr(dependencies, "can_user_share_agent_check", lambda *a, **k: False,
                        raising=False)
    from database import db as core_db
    monkeypatch.setattr(core_db, "can_user_share_agent", lambda u, a: u == "owner")

    canvas_router._gate_human_removal(_user(username="owner"), "agent-a")

    with pytest.raises(HTTPException) as excinfo:
        canvas_router._gate_human_removal(_user(username="stranger"), "agent-a")
    assert excinfo.value.status_code == 403


# --- the Workspace gate -----------------------------------------------------

def test_an_external_client_may_never_manage_canvases(monkeypatch):
    """Whatever their roster says. There is no per-user canvas copy, so the
    ent#548 "unshare your own" middle ground does not exist here."""
    from client_portal import service
    from database import db as core_db
    monkeypatch.setattr(core_db, "get_user_by_email",
                        lambda e: {"username": "alice"})
    monkeypatch.setattr(core_db, "can_user_share_agent", lambda u, a: True)

    assert service.may_manage_canvases(
        "agent-a", "client@example.com", is_platform=False) is False


def test_a_platform_owner_may_manage_canvases(monkeypatch):
    from client_portal import service
    from database import db as core_db
    monkeypatch.setattr(core_db, "get_user_by_email", lambda e: {"username": "alice"})
    monkeypatch.setattr(core_db, "can_user_share_agent", lambda u, a: u == "alice")

    assert service.may_manage_canvases(
        "agent-a", "alice@example.com", is_platform=True) is True


def test_the_workspace_gate_fails_closed_on_an_unknown_email(monkeypatch):
    from client_portal import service
    from database import db as core_db
    monkeypatch.setattr(core_db, "get_user_by_email", lambda e: None)

    assert service.may_manage_canvases(
        "agent-a", "ghost@example.com", is_platform=True) is False
    assert service.may_manage_canvases("agent-a", None, is_platform=True) is False


def test_the_workspace_gate_uses_the_same_predicate_the_operator_gate_does():
    """Not a second implementation that agrees today. Agent Detail and the
    Workspace must never disagree about who owns an agent."""
    import inspect
    from client_portal import service
    source = inspect.getsource(service.may_manage_canvases)
    assert "can_user_share_agent" in source


# --- wiring -----------------------------------------------------------------

def test_bulk_delete_is_declared_above_the_parameterized_canvas_routes():
    """Invariant #4. It works today because the methods differ, and would stop
    working the moment anyone adds a POST on `{canvas_id}` — which is exactly
    the kind of edit nobody connects to a route three screens away."""
    import pathlib
    backend = pathlib.Path(__file__).resolve().parents[2] / "src" / "backend"
    for path, bulk, param in (
        (backend / "routers" / "canvas.py",
         '"/{name}/canvas/bulk-delete"', '"/{name}/canvas/{canvas_id}"'),
        (backend / "client_portal" / "router.py",
         '"/agents/{agent_name}/canvas/bulk-delete"',
         '"/agents/{agent_name}/canvas/{canvas_id}"'),
    ):
        src = path.read_text()
        assert src.index(bulk) < src.index(param), f"{path.name}: bulk-delete declared too late"


def test_the_pinned_column_ships_on_both_migration_tracks():
    """Invariant #9 — a schema change lands on SQLite AND PostgreSQL."""
    import pathlib
    backend = pathlib.Path(__file__).resolve().parents[2] / "src" / "backend"
    sqlite_src = (backend / "db" / "migrations.py").read_text()
    assert "agent_canvases_pinned" in sqlite_src
    assert "ADD COLUMN pinned" in sqlite_src

    alembic = backend / "migrations" / "versions" / "0058_agent_canvases_pinned.py"
    assert alembic.exists(), "no Alembic revision for the pinned column"
    assert "IF NOT EXISTS pinned" in alembic.read_text()


def test_the_agent_facing_tools_cannot_pin():
    """`pinned` is a human's ordering. If an agent could set it, every agent
    would pin everything and the ordering would carry no information."""
    import pathlib
    repo = pathlib.Path(__file__).resolve().parents[2]
    tools = (repo / "src" / "mcp-server" / "src" / "tools" / "canvas.ts").read_text()
    assert "pinned" not in tools
