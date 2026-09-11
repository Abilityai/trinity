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

import types

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


def _set_cap(monkeypatch, value):
    """Set the per-agent canvas cap the LIVE code will actually read.

    Not `monkeypatch.setattr(db.canvas, "CANVAS_MAX_PER_AGENT", ...)`, which is
    what this file did first and which fails in a full-suite run while passing
    in isolation. Some earlier test evicts `db.canvas` from `sys.modules`, so a
    fresh `import db.canvas` hands back a NEW module object while the live
    `db._canvas_ops` is still an instance of the OLD class — whose method reads
    the OLD module's globals. The patch then lands somewhere nothing consults
    and the cap stays at its default, so the "refuses" assertions fail.

    Patching the bound method's own `__globals__` targets whichever module dict
    the running code actually closes over, whether or not an eviction happened.
    Same fix as #2589, for the same reason.
    """
    from database import db
    monkeypatch.setitem(
        type(db._canvas_ops).upsert_canvas.__globals__,
        "CANVAS_MAX_PER_AGENT",
        value,
    )


def _limit_exceeded():
    """The `CanvasLimitExceeded` the LIVE code will actually raise.

    Same eviction hazard as `_set_cap`, one object over: after `db.canvas` is
    evicted and re-imported, `from db.canvas import CanvasLimitExceeded` hands
    back a class from the NEW module while `db._canvas_ops` raises the OLD
    module's class, and `pytest.raises` on the wrong one reports the correct
    refusal as an unexpected exception. Read it from the same globals the cap
    patch targets, so both resolve against whatever the running code closes over.
    """
    from database import db
    return type(db._canvas_ops).upsert_canvas.__globals__["CanvasLimitExceeded"]


def _write(agent: str, canvas_id: str, **kw):
    from database import db
    return db.upsert_agent_canvas(agent, canvas_id, blocks=kw.pop("blocks", []), **kw)


# --- the cap ----------------------------------------------------------------

def test_the_cap_refuses_a_new_canvas_by_name(canvas_db, monkeypatch):
    """The refusal names the count, the limit, and the way out. An agent reads
    this string — "you are full" with no remedy is a dead end."""
    _set_cap(monkeypatch, 3)

    for i in range(3):
        _write("agent-a", f"c{i}")

    with pytest.raises(_limit_exceeded()) as excinfo:
        _write("agent-a", "c3")

    message = str(excinfo.value)
    assert "3" in message and "retire" in message.lower()


def test_updating_an_existing_canvas_is_never_refused_at_the_cap(canvas_db, monkeypatch):
    """The property that makes the cap safe to ship. An agent at its limit must
    still be able to keep its live surfaces current; a cap that blocked updates
    would freeze the fleet's dashboards the moment it bit."""
    _set_cap(monkeypatch, 2)

    _write("agent-a", "c0", title="first")
    _write("agent-a", "c1", title="second")

    updated = _write("agent-a", "c0", title="rewritten")
    assert updated["title"] == "rewritten"


def test_the_cap_is_per_agent_not_global(canvas_db, monkeypatch):
    """One busy agent must not stop another from creating anything."""
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import agent_ownership
    _set_cap(monkeypatch, 2)

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


# --- the default canvas (AC 8) ----------------------------------------------

def test_the_default_canvas_can_be_deleted_and_comes_back_empty(canvas_db):
    """AC 8. `main` is the id both the MCP tools and the voice panel fall back
    to (ent#536), so it is the one most likely to be deleted by accident — and
    the one whose deletion must not strand every writer that assumes it.

    Nothing special-cases it: the row goes, and the next write recreates it
    through the ordinary upsert. There is no dangling reference because the id
    IS the reference — a canvas is addressed, not pointed at.
    """
    from database import db
    from models import DEFAULT_CANVAS_ID

    _write("agent-a", DEFAULT_CANVAS_ID, title="board")
    assert db.delete_agent_canvas("agent-a", DEFAULT_CANVAS_ID) is True
    assert db.get_agent_canvas("agent-a", DEFAULT_CANVAS_ID) is None

    recreated = _write("agent-a", DEFAULT_CANVAS_ID, blocks=[])
    assert recreated["canvas_id"] == DEFAULT_CANVAS_ID
    assert recreated["blocks"] == []
    assert recreated["pinned"] is False


def test_deleting_the_default_canvas_frees_a_slot_against_the_cap(canvas_db, monkeypatch):
    """The remedy the refusal names has to actually work on every canvas,
    including the default one."""
    from database import db
    from models import DEFAULT_CANVAS_ID
    _set_cap(monkeypatch, 2)

    _write("agent-a", DEFAULT_CANVAS_ID)
    _write("agent-a", "other")
    with pytest.raises(_limit_exceeded()):
        _write("agent-a", "third")

    db.delete_agent_canvas("agent-a", DEFAULT_CANVAS_ID)
    assert _write("agent-a", "third")["canvas_id"] == "third"


def test_the_cap_reaches_the_wire_as_a_409(canvas_db, monkeypatch):
    """The review asked for the ROUTE, not only the db layer: prove the
    `CanvasLimitExceeded` an agent's PUT trips comes back as a **409**, not a
    500 from an exception nobody translated.

    Runs the real chain — `routers/canvas.write_canvas` → `canvas_service.
    write_canvas` → `db.upsert_agent_canvas` — with only the rate limiter
    stubbed (Redis-backed; a bounded fixture has none). The refusal names the
    remedy (retire a canvas) and never claims the payload was too large, since
    413 would send the agent shrinking blocks that were never the problem.
    """
    import asyncio
    from fastapi import HTTPException
    from models import CanvasWrite
    from routers import canvas as canvas_router
    from services import rate_limiter

    monkeypatch.setattr(rate_limiter, "enforce", lambda *a, **k: None)
    _set_cap(monkeypatch, 2)
    _write("agent-a", "one")
    _write("agent-a", "two")

    async def put(canvas_id):
        return await canvas_router.write_canvas(
            name="agent-a",
            canvas_id=canvas_id,
            data=CanvasWrite(blocks=[], audience="operator"),
            request=None,
            current_user=_user(agent_name="agent-a"),
        )

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(put("three"))
    assert excinfo.value.status_code == 409
    assert "delete" in excinfo.value.detail.lower() or "retire" in excinfo.value.detail.lower(), \
        excinfo.value.detail

    # And the same PUT against an EXISTING id is an update, never a refusal —
    # otherwise an agent at the cap could no longer redraw its own boards.
    assert asyncio.run(put("two"))["canvas_id"] == "two"


# NOTE (out of scope, recorded rather than fixed): `canvas_service.empty_canvas`
# returns `created_at`/`updated_at` as None while `models.Canvas` declares them
# required `str`, so `Canvas(**empty_canvas(...))` raises. It is LATENT, not
# live — the only caller is `routers/voice.py::get_voice_panel`, which declares
# no `response_model`, so FastAPI returns the dict unvalidated. Adding a
# `response_model=Canvas` there would turn the teardown-window poll into a 500.
# ent#536's key-subset guard is what keeps the two shapes aligned today.


# --- audit ------------------------------------------------------------------

def test_both_delete_routes_are_audited():
    """AC 1 asks for the deletion to be audited, and the single-canvas route is
    the one a person actually clicks."""
    import inspect
    from routers import canvas as canvas_router

    for handler in (canvas_router.clear_canvas, canvas_router.bulk_delete_canvases):
        src = inspect.getsource(handler)
        assert "platform_audit_service.log" in src, f"{handler.__name__} is not audited"
        assert "canvas_id" in src


def test_the_workspace_routes_are_audited_too():
    """The review finding. This guard only ever looked at `routers.canvas`, so
    it passed while the three Workspace twins recorded nothing at all — and
    `docs/user-docs/agents/agent-canvas.md` tells users deletion is audited,
    which made that sentence false for the client-facing surface.

    Checked over the source rather than by driving the routes because the thing
    that went wrong is a route existing with no audit call in it; a behavioural
    test of the three that exist cannot see a fourth added later.
    """
    import inspect
    from client_portal import router as portal_router

    for handler in (portal_router.portal_delete_canvas,
                    portal_router.portal_bulk_delete_canvases,
                    portal_router.portal_pin_canvas):
        src = inspect.getsource(handler)
        assert "_audit_canvas_change" in src, f"{handler.__name__} is not audited"
    # ...and the shared helper is what actually writes the row.
    assert "platform_audit_service.log" in inspect.getsource(
        portal_router._audit_canvas_change)


def test_the_workspace_audit_names_the_OPERATOR_not_the_platform():
    """An audit row that lands under the wrong actor is worse than none.

    `platform_audit_service._resolve_actor` keys `actor_type` off
    `actor_user` / `actor_agent_name` / `mcp_scope` / `mcp_key_id` — NOT off
    `actor_email`. So an email-only call falls through to its last branch and
    the row is written as `actor_type="system"`, `actor_id="trinity-system"`:
    a named operator's Workspace deletion recorded as a platform action,
    invisible to every `actor_type=user` query and to the per-actor filter the
    audit UI offers. The row exists, so nothing fails — it just says the wrong
    thing, which is the failure mode a missing row does not have.

    Asserted against the REAL resolver rather than by reading the call, because
    the defect is entirely in what that function does with the arguments.
    """
    from services.platform_audit_service import PlatformAuditService

    # The shape the fix must not regress to.
    assert PlatformAuditService._resolve_actor(
        actor_user=None, actor_agent_name=None, mcp_scope=None, mcp_key_id=None,
    ) == ("system", "trinity-system", None)

    # ...and the shape it produces now.
    actor = types.SimpleNamespace(id=7, email="op@example.com", username="op")
    kind, actor_id, email = PlatformAuditService._resolve_actor(
        actor_user=actor, actor_agent_name=None, mcp_scope=None, mcp_key_id=None,
    )
    assert (kind, actor_id, email) == ("user", "7", "op@example.com")


def test_the_workspace_audit_resolves_a_real_user_row():
    """The helper must pass `actor_user`, not only `actor_email` — and must not
    let a lookup failure drop the row."""
    import inspect
    from client_portal import router as portal_router

    src = inspect.getsource(portal_router._audit_canvas_change)
    assert "db.get_user_by_email" in src
    assert "actor_user=actor_user" in src
    # Best-effort: the action is already done, so attribution must never raise.
    assert "except Exception" in src
    # The email still rides along, so a miss under-attributes rather than losing it.
    assert "actor_email=principal.email" in src


def test_pinning_is_audited_on_both_surfaces():
    """A pin decides what an entire roster sees first, so it is an
    administrative act on a shared surface — not a per-viewer preference. The
    operator route was the one that recorded nothing."""
    import inspect
    from routers import canvas as canvas_router
    from client_portal import router as portal_router

    assert "platform_audit_service.log" in inspect.getsource(canvas_router.pin_canvas)
    assert "_audit_canvas_change" in inspect.getsource(portal_router.portal_pin_canvas)


def test_an_agent_key_may_not_pin_its_own_canvas():
    """`docs/user-docs/agents/agent-canvas.md` says "the agent cannot pin its
    own canvas". It could: `_gate_human_removal` lets an agent-scoped key act on
    its own canvases (right for delete — tidying up after itself) and pin shared
    that gate. `pinned` being absent from the MCP tools is a property of the
    CLIENT, not of this route, so the doc was describing a convention rather
    than a control. `_gate_pin` is humans-only, which makes it true.
    """
    from fastapi import HTTPException
    from routers import canvas as canvas_router

    with pytest.raises(HTTPException) as excinfo:
        canvas_router._gate_pin(_user(agent_name="agent-a"), "agent-a")
    assert excinfo.value.status_code == 403
    # ...while delete deliberately still allows exactly that.
    canvas_router._gate_human_removal(_user(agent_name="agent-a"), "agent-a")


def test_the_pin_route_uses_the_humans_only_gate():
    import inspect
    from routers import canvas as canvas_router

    src = inspect.getsource(canvas_router.pin_canvas)
    assert "_gate_pin(" in src
    assert "_gate_human_removal(" not in src


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

    alembic = backend / "migrations" / "versions" / "0059_agent_canvases_pinned.py"
    assert alembic.exists(), "no Alembic revision for the pinned column"
    assert "IF NOT EXISTS pinned" in alembic.read_text()


def test_the_agent_facing_tools_cannot_pin():
    """`pinned` is a human's ordering. If an agent could set it, every agent
    would pin everything and the ordering would carry no information."""
    import pathlib
    repo = pathlib.Path(__file__).resolve().parents[2]
    tools = (repo / "src" / "mcp-server" / "src" / "tools" / "canvas.ts").read_text()
    assert "pinned" not in tools


# --- the stated bound actually reaches the client ----------------------------

def test_the_canvas_ceiling_is_on_the_feature_flags_surface():
    """AC: "a stated bound for the canvas pile". It was enforced and unstated —
    `canvasLimit` was a `ref(0)` nothing ever assigned, so `canvasHeadroom(n, 0)`
    returned `{label: null}` and the early warning could not render.

    The ceiling is a CONSTANT, not per-agent state, and the client already holds
    the count, so it rides the established UI-value surface rather than a new
    route (Invariant #13's three surfaces for one integer) or an envelope around
    the canvas list (which the MCP tool and the Workspace both read as a bare
    array).
    """
    import inspect
    from routers import settings as settings_router

    src = inspect.getsource(settings_router.get_public_feature_flags)
    assert '"canvas_max_per_agent": CANVAS_MAX_PER_AGENT' in src
    # ...and it is the same constant the refusal is raised from, not a copy.
    from models import CANVAS_MAX_PER_AGENT
    from db import canvas as canvas_db
    assert canvas_db.CANVAS_MAX_PER_AGENT is CANVAS_MAX_PER_AGENT


def test_the_agent_card_and_the_roster_agree_about_canvas_management():
    """`get_agent_card` omitted `can_manage_canvases`, so the SAME owner read
    `true` in the sidebar and `false` on the agent's own page — two
    representations of one card answering differently, which is the defect
    #2160's docstring says that function exists to prevent. It failed closed (a
    hidden control, never one that 403s), which is why it was latent.
    """
    import inspect
    from client_portal import service as portal_service

    src = inspect.getsource(portal_service.get_agent_card)
    assert "can_manage_canvases=may_manage_canvases(" in src, (
        "the single-card path does not resolve canvas management, so it "
        "disagrees with the roster for the same viewer"
    )
