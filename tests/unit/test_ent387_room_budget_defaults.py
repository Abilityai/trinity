"""Room budget defaults, and who is allowed to set them (ent#387).

ent#381 retired the Sessions page, and `NewRoomDialog` — the only surface that
ever SET a room budget — went with it. The issue reads as a UI placement
question, but the API had the sharper half: `POST /api/rooms` took
`max_messages` / `max_cost_usd` / `ttl_hours` from ANY principal, including a
workspace client (ent#362). The dialog never showed the fields; the endpoint
accepted them. A budget bounds what a customer conversation may spend, so a
client-settable budget is not a control at all.

Pinned here:
  1. a workspace client's supplied budget is IGNORED — not clamped, because a
     clamp still lets the bounded party pick the ceiling;
  2. a platform principal (operator) may still bound a room explicitly;
  3. anything unspecified resolves to the OPERATOR default, not the constant;
  4. a hand-edited settings row cannot install a budget the API would refuse, and
     an unusable row degrades to the code default rather than failing the create.
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def rooms_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-budgets.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))

    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.engine import get_engine
    from db.tables import (
        metadata as m,
        agent_ownership,
        schedule_executions,   # get_room sums room spend through it
        system_settings,
        users,
    )
    m.create_all(
        get_engine(),
        tables=[agent_ownership, users, system_settings, schedule_executions],
    )

    from shared_sessions.schema import init_shared_sessions_schema
    init_shared_sessions_schema()

    from sqlalchemy import insert
    with get_engine().begin() as conn:
        conn.execute(insert(users).values(id=1, username="alice", role="admin",
                                          email="alice@example.com",
                                          created_at="t", updated_at="t"))
        for name in ("agent-a", "agent-b"):
            conn.execute(insert(agent_ownership).values(
                agent_name=name, owner_id=1, created_at="t"))
    yield str(db_file)


@pytest.fixture()
def allow_all(monkeypatch):
    """Open both ACL doors: the platform guard and the portal roster.

    Room creation resolves a workspace client's agent access through the portal
    roster (ent#362) rather than the platform ACL, so a test that stubs only the
    latter never reaches the budget logic it means to exercise.
    """
    import dependencies
    monkeypatch.setattr(dependencies, "assert_agent_access", lambda *a, **k: None)

    import client_portal.service as portal_service
    monkeypatch.setattr(portal_service, "agent_on_roster", lambda *a, **k: True)


def _operator():
    from models import User
    return User(id=1, username="alice", role="admin")


class _WorkspaceClient:
    """The ent#362 principal: an email, no role, no agent name, not platform."""
    email = "client@example.com"
    is_platform = False
    is_portal = True


def _mk_room(caller, **kw):
    from shared_sessions import service
    return service.create_room(caller, kw.pop("name", "Room"), ["agent-a"], **kw)


def _row(room):
    from db.engine import get_engine
    from sqlalchemy import text
    with get_engine().connect() as conn:
        return conn.execute(
            text("SELECT max_messages, max_cost_usd, expires_at FROM enterprise_rooms WHERE id = :i"),
            {"i": room["id"]},
        ).mappings().first()


# --- who may bound a room ---------------------------------------------------

def test_workspace_client_cannot_set_its_own_budget(rooms_db, allow_all):
    """The defect this issue is really about: the bounded party setting the bound."""
    room = _mk_room(_WorkspaceClient(), max_messages=500, max_cost_usd=999.0, ttl_hours=168)
    row = _row(room)

    assert row["max_messages"] == 60, "the client's 500 must not survive"
    assert row["max_cost_usd"] is None, "the client's cost cap must not survive"


def test_operator_may_still_bound_a_room(rooms_db, allow_all):
    room = _mk_room(_operator(), max_messages=10, max_cost_usd=1.5)
    row = _row(room)

    assert row["max_messages"] == 10
    assert float(row["max_cost_usd"]) == 1.5


def test_a_portal_principal_missing_is_platform_fails_closed(rooms_db, allow_all):
    """A portal principal that forgets to carry `is_platform` must not inherit the
    platform default. The absent-attribute default exists for `User` (which has no
    such field at all); a principal marked `is_portal` is a client either way."""
    class _Malformed:
        email = "client@example.com"
        is_portal = True          # no is_platform at all

    row = _row(_mk_room(_Malformed(), max_messages=500))
    assert row["max_messages"] == 60


def test_an_agent_principal_is_treated_as_platform(rooms_db, allow_all):
    """An agent-scoped key is a platform credential — MCP-created rooms carrying
    explicit budgets (the operator's own automation) must keep working."""
    from models import User
    # agent-b creates; agent-a participates. (An agent that rooms with ITSELF
    # collides with its own creator participant row — a pre-existing property,
    # not what this test is about.)
    room = _mk_room(User(id=1, username="alice", role="admin", agent_name="agent-b"),
                    max_messages=7)
    assert _row(room)["max_messages"] == 7


# --- the operator default ---------------------------------------------------

def test_unspecified_budget_uses_the_operator_default(rooms_db, allow_all):
    from database import db
    from shared_sessions import service

    db.set_setting(service.ROOM_DEFAULT_MAX_MESSAGES_KEY, "25")
    db.set_setting(service.ROOM_DEFAULT_MAX_COST_KEY, "2.5")

    row = _row(_mk_room(_operator()))
    assert row["max_messages"] == 25
    assert float(row["max_cost_usd"]) == 2.5


def test_the_default_bounds_a_workspace_client_too(rooms_db, allow_all):
    from database import db
    from shared_sessions import service

    db.set_setting(service.ROOM_DEFAULT_MAX_MESSAGES_KEY, "5")
    row = _row(_mk_room(_WorkspaceClient(), max_messages=500))
    assert row["max_messages"] == 5


def test_ttl_zero_means_no_expiry(rooms_db, allow_all):
    from database import db
    from shared_sessions import service

    db.set_setting(service.ROOM_DEFAULT_TTL_HOURS_KEY, "0")
    assert _row(_mk_room(_operator()))["expires_at"] is None


def test_out_of_range_row_degrades_to_the_code_default(rooms_db, allow_all):
    """A hand-edited row must not install a budget the API would have refused —
    and must not fail room creation either."""
    from database import db
    from shared_sessions import service

    db.set_setting(service.ROOM_DEFAULT_MAX_MESSAGES_KEY, "99999")
    assert _row(_mk_room(_operator()))["max_messages"] == service.DEFAULT_MAX_MESSAGES


def test_unparseable_row_degrades_to_the_code_default(rooms_db, allow_all):
    from database import db
    from shared_sessions import service

    db.set_setting(service.ROOM_DEFAULT_MAX_MESSAGES_KEY, "not-a-number")
    assert _row(_mk_room(_operator()))["max_messages"] == service.DEFAULT_MAX_MESSAGES


def test_zero_cost_row_means_uncapped_not_a_zero_cap(rooms_db, allow_all):
    """0 is the absence of a cap, not a room that closes on its first message —
    the API refuses <= 0 for the same reason."""
    from database import db
    from shared_sessions import service

    db.set_setting(service.ROOM_DEFAULT_MAX_COST_KEY, "0")
    assert _row(_mk_room(_operator()))["max_cost_usd"] is None


def test_a_settings_read_failure_never_blocks_a_room(rooms_db, allow_all, monkeypatch):
    """A room is more important than an operator preference: if the settings read
    fails, creation continues on the code defaults (the tightest of the three
    sources) rather than 500-ing."""
    from database import db
    from shared_sessions import service

    def boom(*_a, **_kw):
        raise RuntimeError("settings down")

    monkeypatch.setattr(db, "get_setting_value", boom)
    assert _row(_mk_room(_operator()))["max_messages"] == service.DEFAULT_MAX_MESSAGES


def test_a_defaults_resolution_failure_is_also_survivable(rooms_db, allow_all, monkeypatch):
    """The belt above the `_setting` guard — whatever the failure's shape."""
    from shared_sessions import service

    monkeypatch.setattr(service, "budget_defaults",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert _row(_mk_room(_operator()))["max_messages"] == service.DEFAULT_MAX_MESSAGES


# --- the setter -------------------------------------------------------------

def test_set_and_clear_round_trip(rooms_db):
    from shared_sessions import service

    state = service.set_budget_defaults(max_messages=30, max_cost_usd=4.0, ttl_hours=2)
    assert (state["max_messages"], state["max_cost_usd"], state["ttl_hours"]) == (30, 4.0, 2)
    assert state["sources"][service.ROOM_DEFAULT_MAX_MESSAGES_KEY] == "db-row"

    cleared = service.set_budget_defaults(clear=[service.ROOM_DEFAULT_MAX_COST_KEY])
    assert cleared["max_cost_usd"] is None
    assert cleared["sources"][service.ROOM_DEFAULT_MAX_COST_KEY] == "code-default"
    assert cleared["max_messages"] == 30, "a partial update must not reset the others"


def test_sources_distinguish_configured_from_inherited(rooms_db):
    from shared_sessions import service

    state = service.budget_defaults()
    assert set(state["sources"].values()) == {"code-default"}

    service.set_budget_defaults(ttl_hours=1)
    state = service.budget_defaults()
    assert state["sources"][service.ROOM_DEFAULT_TTL_HOURS_KEY] == "db-row"
    assert state["sources"][service.ROOM_DEFAULT_MAX_MESSAGES_KEY] == "code-default"
