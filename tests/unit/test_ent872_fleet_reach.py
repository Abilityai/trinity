"""trinity-enterprise#872 — fleet-level routes narrow an agent key to its reach.

ent#629 narrowed every PER-AGENT gate (`dependencies.agent_may_reach`). The
fleet-level reads and writes resolve their roster through two other seams —
`helpers.accessible_agent_names` and `helpers.get_accessible_agents` — which
short-circuit on `role == "admin"`. An agent key carries its owner's role, so on
an admin-owned install those answered it with every agent on the install.

The fix narrows the seams, not the routes: an agent-scoped principal always gets
`dependencies.agent_reach_set` (itself, its edges, the agents it spawned), the
same set the operator queue already used. Every other principal is unchanged.

Driven by the ent#629 ``fleet`` fixture: an admin owner, A→B edge, A spawned
SPAWN, C with no edge, FOREIGN owned by another user.
"""
from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path

import pytest

_UNIT = str(Path(__file__).resolve().parent)
if _UNIT not in sys.path:  # the ent#629 fleet is the shared world
    sys.path.insert(0, _UNIT)

from test_ent629_agent_reach import (  # noqa: F401 — `fleet` is a fixture
    A, B, C, FOREIGN, OTHER, OWNER, SPAWN, _agent_key, _human, _real_app, fleet,
)

from db_harness import db_backend  # noqa: E402,F401 — `fleet` needs it
import dependencies  # noqa: E402
from models import User  # noqa: E402


def _user_key(role: str = "admin") -> User:
    return User(id=1, username=OWNER, role=role, mcp_scope="user")


def _nameless_agent_key() -> User:
    """`get_current_user` admits an agent-scoped key with no agent_name."""
    return User(id=1, username=OWNER, role="admin", mcp_scope="agent")


# ---------------------------------------------------------------------------
# Part A — one reach builder
# ---------------------------------------------------------------------------

def test_the_reach_builder_lives_in_dependencies_and_the_queue_uses_it(fleet):
    import routers.operator_queue as oq

    assert not hasattr(oq, "_agent_reach_set"), "one builder: dependencies.agent_reach_set"
    owner_names = set(fleet.get_accessible_agent_names("", is_admin=True))
    assert dependencies.agent_reach_set(_agent_key(), owner_names, is_admin=True) == {A, B, SPAWN}
    assert oq._accessible_set(_agent_key()) == {A, B, SPAWN}


def test_the_fleet_reach_of_each_principal(fleet):
    assert dependencies.agent_fleet_reach(_agent_key()) == {A, B, SPAWN}
    # Scope first: a nameless agent key is never handed its owner's roster.
    assert dependencies.agent_fleet_reach(_nameless_agent_key()) == set()
    for who in (_human(), _human("user"), _user_key(),
                User(id=1, username=OWNER, role="admin", mcp_scope="system")):
        assert dependencies.agent_fleet_reach(who) is None


# ---------------------------------------------------------------------------
# Part B — the two seams
# ---------------------------------------------------------------------------

@pytest.fixture
def containers(fleet, monkeypatch):
    """Every agent has a container, plus one orphan Docker knows and the DB doesn't."""
    from services.agent_service import helpers

    running = [{"name": n, "status": "running"} for n in (A, B, C, SPAWN, FOREIGN, "orphan")]
    monkeypatch.setattr(helpers, "list_all_agents_fast", lambda: [dict(r) for r in running])
    return running


def test_accessible_agent_names_narrows_an_agent_key(containers):
    from services.agent_service.helpers import accessible_agent_names

    assert accessible_agent_names(_agent_key()) == sorted({A, B, SPAWN})
    assert accessible_agent_names(_nameless_agent_key()) == []


def test_accessible_agent_names_is_unchanged_for_everyone_else(containers):
    from services.agent_service.helpers import accessible_agent_names

    assert accessible_agent_names(_human()) is None
    assert accessible_agent_names(_user_key()) is None


def test_get_accessible_agents_narrows_an_agent_key(containers):
    from services.agent_service.helpers import get_accessible_agents

    names = {a["name"] for a in get_accessible_agents(_agent_key())}
    assert names == {A, B, SPAWN}            # no C, no FOREIGN, no orphan
    assert get_accessible_agents(_nameless_agent_key()) == []


def test_get_accessible_agents_is_unchanged_for_an_admin_person(containers):
    from services.agent_service.helpers import get_accessible_agents

    names = {a["name"] for a in get_accessible_agents(_human())}
    assert names == {A, B, C, SPAWN, FOREIGN, "orphan"}


# ---------------------------------------------------------------------------
# Part C — the routes, through the real routers
# ---------------------------------------------------------------------------

def test_the_fleet_execution_list_carries_only_the_reach(containers):
    from db_harness import seed_execution
    import routers.executions as rex

    for name in (A, C):
        seed_execution(f"s-{name}", agent_name=name, exec_id=f"e-{name}")
    got = {r["agent_name"] for r in _real_app(rex).get("/api/executions?hours=0").json()}
    assert got == {A}
    # A no-arg principal: `_agent_key(agent=...)` would bind `?agent=` itself.
    key = _real_app(rex, user=lambda: _agent_key())
    assert key.get(f"/api/executions?hours=0&agent={C}").json() == []
    assert {r["agent_name"] for r in key.get(f"/api/executions?hours=0&agent={A}").json()} == {A}


def test_the_fleet_execution_list_is_unchanged_for_an_admin_person(containers):
    from db_harness import seed_execution
    import routers.executions as rex

    for name in (A, C):
        seed_execution(f"s-{name}", agent_name=name, exec_id=f"e-{name}")
    got = {r["agent_name"] for r in _real_app(rex, user=_human).get("/api/executions?hours=0").json()}
    assert got == {A, C}


def test_the_fleet_event_list_is_filtered_to_the_reach_in_sql(fleet, monkeypatch):
    import routers.event_subscriptions as res

    seen = []
    monkeypatch.setattr(res.db, "list_agent_events",
                        lambda **kw: seen.append(kw["agent_names"]) or [])
    assert _real_app(res).get("/api/events").status_code == 200
    assert _real_app(res, user=_human).get("/api/events").status_code == 200
    assert seen == [sorted({A, B, SPAWN}), None]
    assert _real_app(res).get(f"/api/events?source_agent={C}").status_code == 403


def test_an_agent_key_cannot_record_a_persons_proactive_consent(fleet):
    import routers.messages as rm

    body = {"email": "client@example.com", "allow_proactive": True}
    r = _real_app(rm).put(f"/api/agents/{A}/shares/proactive", json=body)
    assert r.status_code == 403, r.text
    # A person — `require_person` counts a person's own user-scoped key as one —
    # is unchanged: no such share reads 404, as before.
    for who in (_human, _user_key):
        r = _real_app(rm, user=who).put(f"/api/agents/{A}/shares/proactive", json=body)
        assert r.status_code == 404, (who.__name__, r.text)


def test_an_agent_key_cannot_mint_a_ws_ticket(fleet, monkeypatch):
    import routers.ws_tickets as rw

    monkeypatch.setattr(rw, "mint_ticket", lambda username, scope: "t")
    assert _real_app(rw).post("/api/ws/ticket").status_code == 403
    for who in (_human, _user_key):
        assert _real_app(rw, user=who).post("/api/ws/ticket").status_code == 200


def test_an_agent_key_reads_only_its_own_key_row(fleet, monkeypatch):
    from datetime import datetime, timezone
    from db_models import McpApiKey
    import routers.mcp_keys as rk

    rows = {kid: McpApiKey(id=kid, name=kid, key_prefix="trinity_mcp_x",
                           created_at=datetime.now(timezone.utc), user_id=1,
                           username=OWNER, agent_name=agent,
                           scope="agent" if agent else "user")
            for kid, agent in (("k-a", A), ("k-b", B), ("k-user", None))}
    monkeypatch.setattr(rk.db, "get_mcp_api_key", lambda kid, username: rows.get(kid))
    key = _real_app(rk)
    assert key.get("/api/mcp/keys/k-a").status_code == 200
    for other in ("k-b", "k-user", "k-missing"):     # uniform 404, like a missing key
        r = key.get(f"/api/mcp/keys/{other}")
        assert (r.status_code, r.json()["detail"]) == (404, "MCP API key not found"), other
    person = _real_app(rk, user=_human)
    assert all(person.get(f"/api/mcp/keys/{k}").status_code == 200 for k in rows)


# ---------------------------------------------------------------------------
# Part D — the two sockets
# ---------------------------------------------------------------------------
#
# `/ws` (T2): an agent key may not mint a ticket at all (Part C above) — the
# dispatcher refreshes a `/ws` roster by the OWNER's email, so a reach-scoped
# `/ws` slot would be re-widened on the next share change.
#
# `/ws/events` (T3): scoped to the reach, NOT refused — on the proof below that
# the same refresh never touches a `/ws/events` slot. The handler registers a
# SCOPE_SCOPED slot with no email, and the refresh skips exactly those, so the
# roster really is the connect-time snapshot the contract says it is; the only
# way it changes is the client's own "refresh" command, which re-resolves
# through the same helper as connect.

from services import event_bus as event_bus_mod  # noqa: E402
from services.event_bus import SCOPE_SCOPED, StreamDispatcher  # noqa: E402

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"


def _main_tree():
    return ast.parse((_BACKEND / "main.py").read_text())


def _events_slot(agents):
    """A slot shaped as `FilteredWebSocketManager.connect` registers one for
    `/ws/events`: SCOPE_SCOPED, no email (that shape is pinned below)."""
    async def _send(_payload):  # pragma: no cover — never awaited here
        return None
    return event_bus_mod._ClientSlot(ws=object(), scope=SCOPE_SCOPED, send_func=_send,
                                     is_admin=False, accessible_agents=set(agents))


@pytest.mark.asyncio
async def test_the_background_roster_refresh_never_re_widens_an_events_slot():
    """T3 proof. The resolver answers with the OWNER's whole roster; the slot
    must keep its reach and the resolver must never even be consulted."""
    dispatcher = StreamDispatcher()
    slot = _events_slot({A, B, SPAWN})
    dispatcher._clients = {"events": slot}
    asked = []
    dispatcher.set_accessible_resolver(
        lambda email: asked.append(email) or [A, B, C, SPAWN, FOREIGN])
    for event in ("agent_shared", "agent_created", "agent_deleted", "agent_renamed"):
        dispatcher._maybe_invalidate_rosters({"event": event, "data": {"name": C}})
    await asyncio.gather(*list(dispatcher._refresh_tasks))
    assert asked == []
    assert slot.accessible_agents == {A, B, SPAWN}


def test_events_slots_are_registered_outside_the_email_keyed_refresh():
    """The pin under the proof: `/ws/events` registers SCOPE_SCOPED with no
    email — the two things the refresh skips on. Either one changing would
    put an agent slot back on the owner-email refresh path."""
    cls = next(n for n in ast.walk(_main_tree())
               if isinstance(n, ast.ClassDef) and n.name == "FilteredWebSocketManager")
    connect = next(n for n in ast.walk(cls)
                   if isinstance(n, ast.AsyncFunctionDef) and n.name == "connect")
    register = next(c for c in ast.walk(connect) if isinstance(c, ast.Call)
                    and getattr(c.func, "attr", None) == "register")
    kw = {k.arg: ast.unparse(k.value) for k in register.keywords}
    assert kw["scope"] == "SCOPE_SCOPED"
    assert "email" not in kw


def _key_info(scope="agent", agent=A, email=None):
    return {"scope": scope, "agent_name": agent, "user_id": OWNER, "user_email": email}


def test_the_event_stream_roster_of_an_agent_key_is_its_reach(fleet):
    from services.ws_identity_service import event_stream_roster

    owner = fleet.get_user_by_username(OWNER)
    assert owner["role"] == "admin"
    assert event_stream_roster(_key_info(), owner) == (False, sorted({A, B, SPAWN}))
    # Scope first: a nameless agent key is never handed its owner's roster.
    assert event_stream_roster(_key_info(agent=None), owner) == (False, [])


def test_the_event_stream_roster_of_every_other_key_is_unchanged(fleet):
    from services.ws_identity_service import event_stream_roster

    owner = fleet.get_user_by_username(OWNER)
    for scope in ("user", None):
        assert event_stream_roster(_key_info(scope=scope, agent=None), owner) == (
            True, fleet.get_accessible_agent_names(None, True))
    other = fleet.get_user_by_username(OTHER)
    assert event_stream_roster(_key_info(scope="user", agent=None, email="o@e.com"), other) == (
        False, fleet.get_accessible_agent_names("o@e.com", False))


def test_the_events_handler_resolves_both_rosters_through_the_helper():
    """Connect and the client's "refresh" command both go through
    `event_stream_roster`; the raw owner-roster read is gone from the handler."""
    fn = next(n for n in ast.walk(_main_tree())
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "websocket_events_endpoint")
    calls = [getattr(c.func, "attr", getattr(c.func, "id", None))
             for c in ast.walk(fn) if isinstance(c, ast.Call)]
    assert "get_accessible_agent_names" not in calls
    assert calls.count("event_stream_roster") == 2


# ---------------------------------------------------------------------------
# Part E — the fleet health status (D21)
# ---------------------------------------------------------------------------

@pytest.fixture
def health(containers, monkeypatch):
    import routers.monitoring as rm
    import services.agent_client as ac
    import services.heartbeat_service as hs

    class _Svc:
        is_running = False

    monkeypatch.setattr(rm, "get_monitoring_service", lambda: _Svc())
    monkeypatch.setattr(rm.db, "get_all_latest_health_checks", lambda names, kind: {})
    monkeypatch.setattr(rm.db, "get_health_summary", lambda names: {})
    monkeypatch.setattr(rm, "_merge_sync_health", lambda agents, names: None)
    monkeypatch.setattr(hs, "heartbeat_status_bulk", lambda names: {})
    monkeypatch.setattr(ac, "get_all_circuit_states", lambda: {})
    return rm


def test_the_fleet_health_status_carries_only_the_reach(health):
    """`routers/monitoring.py` short-circuited on `role != "admin"` BEFORE the
    seam, so an admin-owned agent key never reached the narrowing at all."""
    got = _real_app(health).get("/api/monitoring/status").json()
    assert {a["name"] for a in got["agents"]} == {A, B, SPAWN}
    assert got["summary"]["total_agents"] == 3


def test_the_fleet_health_status_is_unchanged_for_an_admin_person(health):
    got = _real_app(health, user=_human).get("/api/monitoring/status").json()
    assert {a["name"] for a in got["agents"]} == {A, B, C, SPAWN, FOREIGN, "orphan"}
