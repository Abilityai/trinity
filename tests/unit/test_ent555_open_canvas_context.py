"""ent#555 — the open canvas is shared context.

When someone with a canvas on screen says "add a column to this", the agent
should act on that canvas. Before this the turn carried the message and nothing
about the surface around it, so the agent asked, guessed, or silently minted a
new canvas beside the one being looked at.

**The whole design rests on one distinction: this is CONTEXT, never AUTHORITY.**
The open-canvas id says *what is being discussed*; it never widens what the
agent may read or write. Two independent halves enforce that, and this file
exists mostly to hold them apart:

* the WRITE side (`validated_open_canvas`) decides what may be stamped on a
  turn. The id is client-supplied, so it is checked against the agent's OWN
  canvases and against what that caller can see — an operator-only canvas is
  invisible to an external client, and another agent's canvas is refused
  outright. Every failure degrades to "nothing open", never to an error and
  never to a wider reach.
* the READ side (`effective_canvas_id`) decides what a tool acts on, and is
  incapable of granting anything: it resolves an id and every later read and
  write goes through the same ownership and audience gates as before.

The precedence — explicit id > open canvas > default — is stated once so all
three tools agree, and it returns WHY so the agent can say which canvas it
wrote to when nobody named one (AC #7).
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def canvas_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-555.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))

    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.engine import get_engine
    from db.tables import (
        metadata as m, agent_canvases, agent_ownership, agent_sharing, users,
        schedule_executions,
    )
    m.create_all(get_engine(), tables=[
        agent_canvases, agent_ownership, agent_sharing, users, schedule_executions,
    ])

    from sqlalchemy import insert
    with get_engine().begin() as conn:
        conn.execute(insert(users).values(id=1, username="owner", role="user",
                                          email="owner@example.com",
                                          created_at="t", updated_at="t"))
        for name in ("mira", "other"):
            conn.execute(insert(agent_ownership).values(
                agent_name=name, owner_id=1, created_at="t"))
    yield str(db_file)


def _canvas(agent="mira", canvas_id="open-items", audience="roster"):
    from database import db
    return db.upsert_agent_canvas(agent, canvas_id, blocks=[], title="Open items",
                                  audience=audience)


def _turn(agent="mira", open_canvas_id=None):
    from database import db
    return db.create_task_execution(agent_name=agent, message="add a column to this",
                                    triggered_by="public",
                                    open_canvas_id=open_canvas_id)


# --- what may be stamped (the write side) -----------------------------------

def test_a_client_may_mark_a_canvas_they_can_see(canvas_db):
    from client_portal import service
    _canvas(audience="roster")
    assert service.validated_open_canvas("mira", "open-items", is_platform=False) == "open-items"


def test_a_client_cannot_mark_an_operator_only_canvas(canvas_db):
    """Otherwise the field is an existence oracle: a client could learn which
    operator-only canvases an agent keeps by seeing which ids come back."""
    from client_portal import service
    _canvas(canvas_id="secret", audience="operator")
    assert service.validated_open_canvas("mira", "secret", is_platform=False) is None


def test_a_platform_principal_may_mark_an_operator_canvas(canvas_db):
    """They can already read it on Agent Detail — narrowing here would hide
    nothing and would break the voice-call canvas, which is `operator`."""
    from client_portal import service
    _canvas(canvas_id="secret", audience="operator")
    assert service.validated_open_canvas("mira", "secret", is_platform=True) == "secret"


def test_another_agents_canvas_is_refused(canvas_db):
    """THE cross-agent property. The field must not be usable to point one
    agent at another agent's surface."""
    from client_portal import service
    _canvas(agent="other", canvas_id="theirs", audience="roster")
    assert service.validated_open_canvas("mira", "theirs", is_platform=True) is None


@pytest.mark.parametrize("bad", ["ghost", "../../etc/passwd", "", None, 12345, "x" * 500])
def test_anything_unrecognised_means_nothing_is_open(canvas_db, bad):
    """Degrades, never errors: a stale or malformed selection is the ordinary
    case (the canvas may have been deleted mid-conversation)."""
    from client_portal import service
    _canvas()
    assert service.validated_open_canvas("mira", bad, is_platform=True) is None


# --- what a tool acts on (the read side) ------------------------------------

def test_an_explicit_id_always_wins(canvas_db):
    from services import canvas_service
    _canvas()
    _canvas(canvas_id="q3-review")
    ex = _turn(open_canvas_id="open-items")
    assert canvas_service.effective_canvas_id("q3-review", ex.id, "mira") == ("q3-review", "explicit")


def test_no_id_acts_on_the_canvas_the_user_has_open(canvas_db):
    """The feature, in one assertion."""
    from services import canvas_service
    _canvas()
    ex = _turn(open_canvas_id="open-items")
    assert canvas_service.effective_canvas_id(None, ex.id, "mira") == ("open-items", "open")


def test_with_nothing_open_it_falls_back_to_the_default(canvas_db):
    from services import canvas_service
    from models import DEFAULT_CANVAS_ID
    ex = _turn(open_canvas_id=None)
    assert canvas_service.effective_canvas_id(None, ex.id, "mira") == (DEFAULT_CANVAS_ID, "default")


def test_an_agent_cannot_read_another_agents_open_canvas(canvas_db):
    """The execution belongs to `mira`; asking as `other` must resolve nothing
    from it, or the field would leak what a different agent is working on."""
    from services import canvas_service
    from models import DEFAULT_CANVAS_ID
    _canvas()
    ex = _turn(open_canvas_id="open-items")
    assert canvas_service.effective_canvas_id(None, ex.id, "other") == (DEFAULT_CANVAS_ID, "default")


def test_a_canvas_deleted_mid_turn_does_not_get_resurrected(canvas_db):
    """ent#553 made deleting a canvas one click. If a stamped id survived its
    canvas, the agent's next write would CREATE a canvas under that id —
    silently bringing back something a person deleted."""
    from database import db
    from services import canvas_service
    from models import DEFAULT_CANVAS_ID
    _canvas()
    ex = _turn(open_canvas_id="open-items")
    db.delete_agent_canvas("mira", "open-items")
    assert canvas_service.effective_canvas_id(None, ex.id, "mira") == (DEFAULT_CANVAS_ID, "default")


def test_the_resolution_says_why_so_the_agent_can_say_which(canvas_db):
    """AC #7 — with nothing named, "I updated the canvas" is not good enough
    when the user has eight and is looking at one."""
    from services import canvas_service
    _canvas()
    assert canvas_service.effective_canvas_id(None, _turn("mira", "open-items").id, "mira")[1] == "open"
    assert canvas_service.effective_canvas_id("named", None, "mira")[1] == "explicit"
    assert canvas_service.effective_canvas_id(None, None, "mira")[1] == "default"


# --- the turn carries it ----------------------------------------------------

def test_the_open_canvas_round_trips_on_the_execution(canvas_db):
    from database import db
    ex = _turn(open_canvas_id="open-items")
    assert db.get_execution(ex.id).open_canvas_id == "open-items"


def test_the_prompt_names_the_open_canvas():
    """The tool default covers a call with no id, but an agent must READ a
    canvas before editing it and cannot read what it cannot name — so the id
    has to reach the prompt, not only the tool's fallback."""
    import inspect
    from client_portal import service
    src = inspect.getsource(service.portal_chat)
    assert "canvas_prefix" in src
    assert "open on screen" in src


def test_the_prompt_context_survives_a_resumed_turn():
    """The open canvas changes between turns while the session's memory of it
    does not, so replaying it only on a cold turn would leave a resumed
    conversation editing whatever was open first."""
    import inspect
    from client_portal import service
    src = inspect.getsource(service.portal_chat)
    # #2694 put the voice delta at the head of a resumed turn; the canvas rides
    # behind it and AHEAD of the manifest on both arms, so a resumed turn and a
    # cold one name the same canvas in the same place.
    assert "(delta_prefix + canvas_prefix + manifest_prefix + message) if resuming" in src
    assert "cold_message = history_prefix + canvas_prefix + manifest_prefix + message" in src


# --- wiring -----------------------------------------------------------------

def test_the_context_route_is_declared_above_the_canvas_id_routes():
    """Invariant #4 — "context" is a valid canvas-id shape."""
    import pathlib
    backend = pathlib.Path(__file__).resolve().parents[2] / "src" / "backend"
    src = (backend / "routers" / "canvas.py").read_text()
    assert src.index('"/{name}/canvas/context"') < src.index('"/{name}/canvas/{canvas_id}"')


def test_the_column_ships_on_both_migration_tracks():
    """Invariant #9."""
    import pathlib
    backend = pathlib.Path(__file__).resolve().parents[2] / "src" / "backend"
    assert "execution_open_canvas" in (backend / "db" / "migrations.py").read_text()
    alembic = backend / "migrations" / "versions" / "0061_execution_open_canvas.py"
    assert alembic.exists() and "open_canvas_id" in alembic.read_text()


def test_both_turn_paths_carry_the_selection():
    """A field honoured by only the streaming path brings the bug back exactly
    when streaming fails and the sync fallback runs."""
    import inspect
    from client_portal import service
    for fn in (service.portal_chat, service.start_portal_turn):
        assert "open_canvas_id" in inspect.signature(fn).parameters, fn.__name__


def test_the_mcp_tools_resolve_rather_than_defaulting_to_main():
    """AC #3 — a call with no canvas_id must reach the open canvas, not `main`."""
    import pathlib
    repo = pathlib.Path(__file__).resolve().parents[2]
    tools = (repo / "src/mcp-server/src/tools/canvas.ts").read_text()
    assert "resolveCanvasId" in tools
    # the old unconditional fallback must be gone from the three read/write tools
    assert "params.canvas_id || DEFAULT_CANVAS_ID" not in tools
