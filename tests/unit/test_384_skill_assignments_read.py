"""ent#384 — `GET /api/skills/assignments`, the fleet skill→agents read.

Backs the Library's Skills tab, where every skill block names the agents that
already hold it. One batched read rather than one call per block (the ent#260
rule: N+1 mount loops are deleted, not migrated).

Most of this file pins the two things that are easy to get subtly wrong and
impossible to see from the UI:

1. **The access boundary.** Unscoped, this endpoint is a fleet-wide agent-name
   enumeration oracle for any authenticated `role=user` — the Invariant #8
   disclosure class already called out for `GET /api/subscriptions`. Admin
   reads it unfiltered; everyone else sees owned ∪ shared.

2. **Every way it could answer "nobody holds anything" when that is false.**
   An empty map is indistinguishable from a wrong empty map at the UI, so the
   wrong ones have to be excluded here: the admin/empty-set tri-state
   collapse, and — the reason this endpoint does not use
   `accessible_agent_names` — a Docker fault silently emptying the access set.
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db_harness import db_backend  # noqa: E402,F401  (pytest fixture)

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


@dataclass
class _Admin:
    id: int = 1
    username: str = "admin"
    email: Optional[str] = "admin@example.com"
    role: str = "admin"
    agent_name: Optional[str] = None
    connector_agent: Optional[str] = None
    mcp_scope: Optional[str] = None


@dataclass
class _User:
    id: int = 2
    username: str = "bob"
    email: Optional[str] = "bob@example.com"
    role: str = "user"
    agent_name: Optional[str] = None
    connector_agent: Optional[str] = None
    mcp_scope: Optional[str] = None


@dataclass
class _AgentPrincipal:
    """An agent-scoped MCP key resolves to its OWNER, carrying the owner's role
    (ent#293). On a default admin-owned install that is `role="admin"` — which
    is the whole reason `require_admin`-style gates are not enough and
    `reject_agent_principal` exists."""
    id: int = 1
    username: str = "admin"
    email: Optional[str] = "admin@example.com"
    role: str = "admin"
    agent_name: Optional[str] = "scout"
    connector_agent: Optional[str] = None
    mcp_scope: Optional[str] = "agent"


# Fleet fixture: `scout` and `scribe` are bob's, `sage` is someone else's.
_ROWS = [
    {"skill_name": "research", "agent_name": "sage", "display_label": "Sage"},
    {"skill_name": "research", "agent_name": "scout", "display_label": None},
    {"skill_name": "writing", "agent_name": "scribe", "display_label": "Scribe"},
]

_METADATA = {
    "scout": {"owner_username": "bob", "is_shared_with_user": False},
    "scribe": {"owner_username": "carol", "is_shared_with_user": True},
    "sage": {"owner_username": "carol", "is_shared_with_user": False},
}


@pytest.fixture
def router_mod():
    try:
        from routers import skills as mod
    except ImportError:  # pragma: no cover - backend venv required
        pytest.skip("backend venv required")
    return mod


def _call(mod, user):
    return asyncio.run(mod.get_skill_assignments(current_user=user))


def _wire(monkeypatch, mod, rows=None, metadata=None):
    monkeypatch.setattr(
        mod.db, "get_all_skill_assignments",
        lambda: list(_ROWS if rows is None else rows),
    )
    monkeypatch.setattr(
        mod.db, "get_all_agent_metadata",
        lambda email="": dict(_METADATA if metadata is None else metadata),
    )


# ---------------------------------------------------------------------------
# The access boundary
# ---------------------------------------------------------------------------

def test_admin_sees_every_holder_unfiltered(router_mod, monkeypatch):
    _wire(monkeypatch, router_mod)

    res = _call(router_mod, _Admin())

    assert res.scope == "all"
    assert sorted(a.name for a in res.assignments["research"]) == ["sage", "scout"]
    assert [a.name for a in res.assignments["writing"]] == ["scribe"]


def test_non_admin_sees_only_owned_and_shared(router_mod, monkeypatch):
    """`sage` belongs to carol and is not shared with bob, so bob must not learn
    it exists — the enumeration-oracle boundary."""
    _wire(monkeypatch, router_mod)

    res = _call(router_mod, _User())

    assert res.scope == "accessible"
    assert [a.name for a in res.assignments["research"]] == ["scout"]   # owned
    assert [a.name for a in res.assignments["writing"]] == ["scribe"]   # shared
    every_name = {a.name for agents in res.assignments.values() for a in agents}
    assert "sage" not in every_name


def test_empty_accessible_set_does_not_collapse_into_admin_unfiltered(
    router_mod, monkeypatch
):
    """The tri-state trap. `None` means admin (no filter); `set()` means a real
    non-admin who can reach no agent at all. A falsy check (`if visible:`)
    treats them identically and hands that user the entire fleet."""
    _wire(monkeypatch, router_mod, metadata={})

    res = _call(router_mod, _User())

    assert res.scope == "accessible"
    assert res.assignments == {}


def test_agent_principal_is_rejected(router_mod, monkeypatch):
    """An agent key inherits its owner's role, so without this gate every
    agent's injected TRINITY_MCP_API_KEY would read the unfiltered fleet
    capability map on a default admin-owned install. Free to enforce because
    there is deliberately no MCP tool and no agent consumer — if one is ever
    added, this is the test that has to be revisited first."""
    from fastapi import HTTPException

    _wire(monkeypatch, router_mod)

    with pytest.raises(HTTPException) as exc:
        _call(router_mod, _AgentPrincipal())
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# Every route to a WRONG empty answer
# ---------------------------------------------------------------------------

def test_access_set_is_db_derived_not_docker_derived(router_mod):
    """The reason this endpoint does not use `accessible_agent_names`.

    That helper resolves through `list_all_agents_fast()`, a Docker
    `containers.list()` that returns `[]` when the daemon is unreachable or the
    socket is denied — surfaced only as a throttled WARNING. Routed through it,
    changing DOCKER_GID would tell every non-admin operator that no agent holds
    any skill, fleet-wide, with no error state anywhere.

    Asserted STRUCTURALLY rather than by monkeypatching a Docker sentinel —
    the behavioural half is already covered by the owned-and-shared test
    above, which passes with a DB-derived set and no Docker at all. #471
    extracted the pure-DB helper to `services.agent_service.helpers.
    visible_agent_names` (ONE home; second consumer: the subscription-pressure
    batch endpoint), so the router now legitimately imports that module and
    the guard pins the *property* rather than the module edge: the router's
    alias IS the shared helper, the helper reads the DB batch, and neither the
    helper nor the router references the Docker-derived
    `accessible_agent_names`.
    """
    import inspect

    src = inspect.getsource(router_mod._visible_agent_names)
    assert "get_all_agent_metadata" in src, (
        "the access set must come from the pure-DB batch read"
    )
    assert "accessible_agent_names" not in src.split('"""')[-1], (
        "accessible_agent_names is Docker-derived and returns [] on any Docker "
        "fault; using it here reintroduces the fleet-wide false-empty"
    )

    # The alias must resolve to the ONE shared helper (a local
    # re-implementation would dodge both asserts above the moment the helper
    # moved), and the router itself must never touch the Docker-derived access
    # helper.
    #
    # Pinned by ORIGIN (`__module__`/`__qualname__`), deliberately NOT by
    # object identity against a freshly-imported `helpers`. `router_mod` is the
    # module object bound at ITS import, while a `from services.agent_service
    # import helpers` inside this test resolves at call time — and the suite
    # contains tests that purge `sys.modules` (the reason CI carries a
    # dedicated sys.modules-pollution lint). After such a purge the module is
    # re-imported from the same path under the same key, so `is` compares two
    # distinct function objects and fails for a reason that says nothing about
    # the property under test: the assert passed alone and failed on all three
    # full-suite seed orders. `__module__` still defeats what identity was
    # there to defeat — a router-local `def _visible_agent_names` would read
    # `routers.skills`.
    assert (
        router_mod._visible_agent_names.__module__
        == "services.agent_service.helpers"
        and router_mod._visible_agent_names.__qualname__ == "visible_agent_names"
    ), (
        "the router must bind services.agent_service.helpers.visible_agent_names "
        "— the pure-DB visible-set rule has ONE home (#471); got "
        f"{router_mod._visible_agent_names.__module__}."
        f"{router_mod._visible_agent_names.__qualname__}"
    )
    router_src = inspect.getsource(router_mod)
    assert "accessible_agent_names" not in router_src


def test_a_skill_with_no_holders_is_absent_not_empty(router_mod, monkeypatch):
    """Absent, so the client's own `|| []` default owns the zero. An explicit
    empty list per library skill would require the endpoint to know the library
    contents, which is the skill service's job, not the DB's."""
    _wire(monkeypatch, router_mod, rows=[])

    res = _call(router_mod, _Admin())

    assert res.assignments == {}


# ---------------------------------------------------------------------------
# Response shape — an allow-list, asserted on KEY ABSENCE
# ---------------------------------------------------------------------------

def test_response_model_is_an_allowlist_not_a_passthrough(router_mod, monkeypatch):
    """The ent#334 rule, from this same router: asserting "the sensitive value
    is not in the body" is not enough, because the next release changes the
    value. Assert the KEY cannot appear.

    The db layer selects from `agent_ownership`, whose columns include
    subscription ids, resource limits and encrypted-credential pointers. If the
    accessor ever widens, the model must drop the extras rather than ship them.
    """
    leaky = [
        {
            "skill_name": "research",
            "agent_name": "scout",
            "display_label": "Scout",
            "subscription_id": "sub_should_never_ship",
            "avatar_identity_prompt": "secret",
            "owner_id": 7,
        }
    ]
    _wire(monkeypatch, router_mod, rows=leaky)

    res = _call(router_mod, _Admin())
    agent = res.assignments["research"][0]

    assert set(agent.model_dump().keys()) == {"name", "display_label"}
    assert agent.display_label == "Scout"


def test_display_label_none_is_preserved_not_coerced(router_mod, monkeypatch):
    """NULL means "render the slug" (ent#181). Coercing it to the name here
    would make a labelled and an unlabelled agent indistinguishable to the
    client, which needs the difference for its hover title."""
    _wire(monkeypatch, router_mod)

    res = _call(router_mod, _Admin())
    scout = next(a for a in res.assignments["research"] if a.name == "scout")

    assert scout.display_label is None


# ---------------------------------------------------------------------------
# Route registration (Invariant #4)
# ---------------------------------------------------------------------------

def test_assignments_route_is_not_shadowed_by_a_parameterized_sibling(router_mod):
    """`/skills/library/{skill_name}` and `/skills/sources/{source_id}` are
    parameterized siblings under the same prefix. They cannot capture
    `/skills/assignments` (the second segment is a literal in each), but this is
    the invariant that has bitten this codebase repeatedly, so pin it: the path
    must exist exactly once and resolve to this handler.
    """
    matches = [
        r for r in router_mod.router.routes
        if getattr(r, "path", None) == "/api/skills/assignments"
    ]
    assert len(matches) == 1, "expected exactly one /api/skills/assignments route"
    assert matches[0].endpoint is router_mod.get_skill_assignments
    assert "GET" in matches[0].methods

    # And nothing declared earlier is a single-segment catch-all under /skills
    # that would swallow it (e.g. a future `/skills/{name}`).
    ours = router_mod.router.routes.index(matches[0])
    for earlier in router_mod.router.routes[:ours]:
        path = getattr(earlier, "path", "") or ""
        assert path != "/api/skills/{skill_name}", (
            f"{path} is declared before /api/skills/assignments and would capture it"
        )


# ---------------------------------------------------------------------------
# The db-layer predicate: rows that must never count as a holder
# ---------------------------------------------------------------------------

class TestHolderPredicate:
    """The db-layer filters, exercised against a REAL migrated database.

    An earlier version of this class matched `inspect.getsource` for substrings
    like `"agent_skills.join("` and `"is_ephemeral"`. That is worse than no
    test: `"agent_skills.join("` also matches a LEFT OUTER JOIN, and
    `"is_ephemeral"` also matches the *inverted* predicate — so mutating the
    accessor to `isouter=True` plus `is_ephemeral == 1` (returning ONLY ghosts,
    the exact opposite of the contract) still passed every assertion. A test
    whose docstring claims to guard load-bearing filters, and which survives
    their inversion, manufactures confidence.

    Runs on SQLite always and on PostgreSQL when TEST_POSTGRES_URL is set
    (`db_backend`, #300), which also covers the Core-vs-`text()` concern by
    construction rather than by grepping for a substring.
    """

    def test_only_live_non_ghost_owned_rows_count_as_holders(self, db_backend):
        from db.skills import SkillsOperations
        from db_harness import run as hrun

        # One agent per case, each holding the same skill so the returned set
        # is exactly "which filters let a row through".
        hrun(
            "INSERT INTO agent_ownership (agent_name, owner_id, created_at, deleted_at, is_ephemeral, display_label) "
            "VALUES ('live', 1, '2026-01-01T00:00:00Z', NULL, 0, 'Live One')"
        )
        hrun(
            "INSERT INTO agent_ownership (agent_name, owner_id, created_at, deleted_at, is_ephemeral, display_label) "
            "VALUES ('legacy-null-ghost-flag', 1, '2026-01-01T00:00:00Z', NULL, NULL, NULL)"
        )
        hrun(
            "INSERT INTO agent_ownership (agent_name, owner_id, created_at, deleted_at, is_ephemeral, display_label) "
            "VALUES ('soft-deleted', 1, '2026-01-01T00:00:00Z', '2026-02-01T00:00:00Z', 0, NULL)"
        )
        hrun(
            "INSERT INTO agent_ownership (agent_name, owner_id, created_at, deleted_at, is_ephemeral, display_label) "
            "VALUES ('ghost', 1, '2026-01-01T00:00:00Z', NULL, 1, NULL)"
        )
        for agent in ("live", "legacy-null-ghost-flag", "soft-deleted", "ghost", "no-ownership-row"):
            hrun(
                "INSERT INTO agent_skills (agent_name, skill_name, assigned_by, assigned_at) "
                "VALUES (:a, 'research', 'admin', '2026-01-01T00:00:00Z')",
                a=agent,
            )

        rows = SkillsOperations().get_all_skill_assignments()
        holders = {r["agent_name"] for r in rows}

        # `live` — the ordinary case.
        assert "live" in holders
        # NULL `is_ephemeral` is every agent created before ent#69. A bare
        # `is_ephemeral == 0` drops all of them from every holder list.
        assert "legacy-null-ghost-flag" in holders
        # #834 preserves a soft-deleted agent's child rows for up to 180 days,
        # and admins read this endpoint unfiltered (ent#335).
        assert "soft-deleted" not in holders
        # A ghost is hard-discarded at budget; its chip would 404 in minutes.
        assert "ghost" not in holders
        # A row whose ownership row is gone is a cascade orphan (canary L-03),
        # not a holder — the INNER JOIN must drop it.
        assert "no-ownership-row" not in holders

        assert holders == {"live", "legacy-null-ghost-flag"}

    def test_display_label_rides_along_and_null_is_preserved(self, db_backend):
        """NULL means "render the slug" (ent#181); coercing it here would make a
        labelled and an unlabelled agent indistinguishable to the client."""
        from db.skills import SkillsOperations
        from db_harness import run as hrun

        hrun(
            "INSERT INTO agent_ownership (agent_name, owner_id, created_at, display_label) "
            "VALUES ('labelled', 1, '2026-01-01T00:00:00Z', 'Scout — Market Research')"
        )
        hrun(
            "INSERT INTO agent_ownership (agent_name, owner_id, created_at, display_label) "
            "VALUES ('unlabelled', 1, '2026-01-01T00:00:00Z', NULL)"
        )
        for agent in ("labelled", "unlabelled"):
            hrun(
                "INSERT INTO agent_skills (agent_name, skill_name, assigned_by, assigned_at) "
                "VALUES (:a, 'writing', 'admin', '2026-01-01T00:00:00Z')",
                a=agent,
            )

        by_name = {
            r["agent_name"]: r["display_label"]
            for r in SkillsOperations().get_all_skill_assignments()
        }
        assert by_name["labelled"] == "Scout — Market Research"
        assert by_name["unlabelled"] is None

    def test_one_row_per_assignment_the_join_cannot_multiply(self, db_backend):
        """`agent_ownership.agent_name` is unique, so the join must be 1:1. A
        duplicated holder would silently inflate every count on the page."""
        from db.skills import SkillsOperations
        from db_harness import run as hrun

        hrun(
            "INSERT INTO agent_ownership (agent_name, owner_id, created_at) "
            "VALUES ('solo', 1, '2026-01-01T00:00:00Z')"
        )
        for skill in ("alpha", "beta"):
            hrun(
                "INSERT INTO agent_skills (agent_name, skill_name, assigned_by, assigned_at) "
                "VALUES ('solo', :s, 'admin', '2026-01-01T00:00:00Z')",
                s=skill,
            )

        rows = SkillsOperations().get_all_skill_assignments()
        assert len(rows) == 2
        assert sorted(r["skill_name"] for r in rows) == ["alpha", "beta"]
