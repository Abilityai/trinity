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

import sys
from pathlib import Path

import pytest

_UNIT = str(Path(__file__).resolve().parent)
if _UNIT not in sys.path:  # the ent#629 fleet is the shared world
    sys.path.insert(0, _UNIT)

from test_ent629_agent_reach import (  # noqa: F401 — `fleet` is a fixture
    A, B, C, FOREIGN, OWNER, SPAWN, _agent_key, _human, _real_app, fleet,
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


@pytest.mark.xfail(strict=True, reason="CP2: seam narrowing")
def test_accessible_agent_names_narrows_an_agent_key(containers):
    from services.agent_service.helpers import accessible_agent_names

    assert accessible_agent_names(_agent_key()) == sorted({A, B, SPAWN})
    assert accessible_agent_names(_nameless_agent_key()) == []


def test_accessible_agent_names_is_unchanged_for_everyone_else(containers):
    from services.agent_service.helpers import accessible_agent_names

    assert accessible_agent_names(_human()) is None
    assert accessible_agent_names(_user_key()) is None


@pytest.mark.xfail(strict=True, reason="CP2: seam narrowing")
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

@pytest.mark.xfail(strict=True, reason="CP2: seam narrowing")
def test_the_fleet_execution_list_carries_only_the_reach(containers):
    from db_harness import seed_execution
    import routers.executions as rex

    for name in (A, C):
        seed_execution(f"s-{name}", agent_name=name, exec_id=f"e-{name}")
    got = {r["agent_name"] for r in _real_app(rex).get("/api/executions?hours=0").json()}
    assert got == {A}
    assert _real_app(rex).get(f"/api/executions?hours=0&agent={C}").json() == []


def test_the_fleet_execution_list_is_unchanged_for_an_admin_person(containers):
    from db_harness import seed_execution
    import routers.executions as rex

    for name in (A, C):
        seed_execution(f"s-{name}", agent_name=name, exec_id=f"e-{name}")
    got = {r["agent_name"] for r in _real_app(rex, user=_human).get("/api/executions?hours=0").json()}
    assert got == {A, C}


@pytest.mark.xfail(strict=True, reason="CP3: consent is a person's own record")
def test_an_agent_key_cannot_record_a_persons_proactive_consent(fleet):
    import routers.messages as rm

    body = {"email": "client@example.com", "allow_proactive": True}
    for who in (_agent_key, _user_key):
        r = _real_app(rm, user=who).put(f"/api/agents/{A}/shares/proactive", json=body)
        assert r.status_code == 403, (who.__name__, r.text)
    # A person is unchanged: no such share reads 404, as before.
    r = _real_app(rm, user=_human).put(f"/api/agents/{A}/shares/proactive", json=body)
    assert r.status_code == 404, r.text


@pytest.mark.xfail(strict=True, reason="CP3: agent keys never mint a /ws ticket")
def test_an_agent_key_cannot_mint_a_ws_ticket(fleet, monkeypatch):
    import routers.ws_tickets as rw

    monkeypatch.setattr(rw, "mint_ticket", lambda username, scope: "t")
    assert _real_app(rw).post("/api/ws/ticket").status_code == 403
    for who in (_human, _user_key):
        assert _real_app(rw, user=who).post("/api/ws/ticket").status_code == 200
