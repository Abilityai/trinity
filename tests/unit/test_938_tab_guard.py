"""Operations › Evaluations tab — the guard is the API (trinity-enterprise#938, C1).

The tab (`views/Operations.vue` → `components/EvaluationsPanel.vue`) issues the
ent#937 aggregate read through `stores/evaluations.js`; this file drives that
exact request set through the real `routers.evaluations` router on the ent#937
harness under the four principals the DoD names, so "Admin / operator only" is
a property of the API and never of UI hiding alone (ruling T1, 2026-10-18):

  * an admin person reads the whole fleet, with the judge keys;
  * an operator-role person (the floor's lowest rung) reads the agents it can
    access, with the judge keys — the floor admits `operator`, not only `admin`;
  * a Workspace-only person (role `user`) is refused by the operator floor on
    every URL the tab issues — 403 `workspace_only`;
  * an agent key never reads the fleet: its own agent's rows only, and no
    judge key, exactly as FR-12 pins for the routes themselves.

The store spec (`evaluationsStore.spec.js`) pins that the store issues only the
URLs listed in `TAB_QUERIES` below; the two together are the AC's "route guard
tested at the API".

Mutation (recorded in the commit): making `dependencies.is_workspace_only_role`
return False for every role turns the `user`-role test red (the floor stops
refusing); dropping the `evaluated`-count scope narrowing would turn the agent
key test red.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

# The ent#937 harness is a sibling test module (the unit island imports by
# file, so the directory must be on the path — the test_2392 precedent).
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_937_routes import (  # noqa: E402,F401 — the harness fixtures register on import
    ADMIN, AGENTS, OWNER, _agent_key, _user, client, db_backend, fleet,
)

pytestmark = pytest.mark.unit

# The request set the tab issues in C1 — one aggregate read per period shape.
# `since`/`until` are the store's ISO-Z bounds; a 30-day window is `custom`
# because the API's named windows are `day` and `week` only.
TAB_QUERIES = [
    ("/api/evaluations/aggregate", {"by": "agent", "period": "week"}),
    ("/api/evaluations/aggregate", {"by": "agent", "period": "custom",
                                    "since": "2026-09-18T00:00:00Z",
                                    "until": "2026-10-18T00:00:00Z"}),
]
JUDGE_KEYS = {"quality_mean", "judge_human_agreement", "agreement_n"}


def _tab_routes():
    from routers import evaluations
    paths = {p for p, _ in TAB_QUERIES}
    routes = [r for r in evaluations.router.routes if getattr(r, "path", None) in paths]
    assert {r.path for r in routes} == paths, "a tab URL does not resolve to a route"
    return routes


def test_admin_person_reads_the_whole_fleet_with_the_judge_keys(fleet):
    fleet.as_user(_user(ADMIN, role="admin"))
    for url, params in TAB_QUERIES:
        r = fleet.get(url, params=params)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["by"] == "agent"
        if params["period"] == "week":
            assert {g["key"]["agent"] for g in body["groups"]} == set(AGENTS)
            assert body["total"]["evaluated"] == 4
            assert JUDGE_KEYS <= set(body["total"])      # a person reads the score


def test_operator_role_person_is_at_the_floor_and_reads_its_own_agents(fleet):
    # The floor's lowest admitted rung. The DoD says "Admin / operator only";
    # `health` is admin-only, this tab is not — an operator who owns agents
    # reads them, never another tenant's, with the judge keys.
    fleet.as_user(_user(OWNER, role="operator"))
    r = fleet.get(*[TAB_QUERIES[0][0]], params=TAB_QUERIES[0][1])
    assert r.status_code == 200, r.text
    body = r.json()
    assert {g["key"]["agent"] for g in body["groups"]} == {"agent-a", "agent-b"}
    assert body["total"]["evaluated"] == 3
    assert JUDGE_KEYS <= set(body["total"])


def test_workspace_only_person_is_refused_on_every_tab_url():
    import dependencies
    routes = _tab_routes()
    for route in routes:
        with pytest.raises(HTTPException) as exc:
            dependencies.enforce_operator_floor(SimpleNamespace(scope={"route": route}),
                                                _user("member", role="user"))
        assert exc.value.status_code == 403
        assert exc.value.detail["code"] == "workspace_only"
    # and the rung just above is admitted — the floor is `operator`, not `admin`
    for route in routes:
        dependencies.enforce_operator_floor(SimpleNamespace(scope={"route": route}),
                                            _user("op", role="operator"))


def test_agent_key_never_reads_the_fleet(fleet):
    fleet.as_user(_agent_key("agent-a"))
    for url, params in TAB_QUERIES:
        r = fleet.get(url, params=params)
        assert r.status_code == 200, r.text
        body = r.json()
        assert [g["key"]["agent"] for g in body["groups"]] in ([], ["agent-a"])
        for summary in [body["total"], *body["groups"]]:
            assert not (JUDGE_KEYS & set(summary))       # a machine reads no score
    week = fleet.get(TAB_QUERIES[0][0], params=TAB_QUERIES[0][1]).json()
    assert week["total"]["evaluated"] == 2                 # a1, a2 — never b1 / c1


def test_the_floor_is_wired_into_the_real_resolver():
    # The harness overrides `get_current_user`, so the floor above is called
    # directly; this pins that production reaches it from the resolver at all
    # (the #3120 class: a guard that is tested but never wired).
    import inspect
    import dependencies
    src = inspect.getsource(dependencies.get_current_user)
    assert "enforce_operator_floor(" in src
