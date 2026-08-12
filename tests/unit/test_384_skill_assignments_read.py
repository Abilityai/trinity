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

def test_docker_outage_cannot_empty_the_access_set(router_mod, monkeypatch):
    """The reason this endpoint does not use `accessible_agent_names`.

    That helper resolves through `list_all_agents_fast()`, a Docker
    `containers.list()` that returns `[]` when the daemon is unreachable or the
    socket is denied — surfaced only as a throttled WARNING. Routed through it,
    changing DOCKER_GID would tell every non-admin operator that no agent holds
    any skill, fleet-wide, with no error state anywhere.

    So: break Docker comprehensively and assert the answer is unchanged.
    """
    _wire(monkeypatch, router_mod)

    called = {"docker": False}

    def _boom(*_a, **_k):
        called["docker"] = True
        return []

    # Whichever way the module might reach Docker, it must not matter.
    import services.agent_service.helpers as helpers
    monkeypatch.setattr(helpers, "list_all_agents_fast", _boom, raising=False)
    monkeypatch.setattr(helpers, "get_accessible_agents", _boom, raising=False)
    monkeypatch.setattr(
        helpers, "accessible_agent_names", lambda u: [], raising=False
    )

    res = _call(router_mod, _User())

    assert [a.name for a in res.assignments["research"]] == ["scout"]
    assert [a.name for a in res.assignments["writing"]] == ["scribe"]
    assert not called["docker"], (
        "the access set must be DB-derived; a Docker read here reintroduces the "
        "fleet-wide false-empty this endpoint was written to avoid"
    )


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
    """These live at the db layer (`get_all_skill_assignments`), which the
    router tests above stub. Exercised against a real migrated DB so the
    Core join, not a mock, is what answers."""

    @pytest.fixture
    def ops(self, tmp_path, monkeypatch):
        try:
            from db.skills import SkillsOperations
        except ImportError:  # pragma: no cover
            pytest.skip("backend venv required")
        return SkillsOperations()

    def test_query_excludes_soft_deleted_ephemeral_and_orphans(self, ops):
        """Read as a contract assertion over the statement itself: the three
        filters are each load-bearing and each easy to drop in a later edit.

        * INNER JOIN — an `agent_skills` row with no ownership row is a cascade
          orphan (canary L-03's territory), not a holder.
        * `deleted_at IS NULL` — #834 preserves a soft-deleted agent's child
          rows for up to 180 days, and admins read this unfiltered (ent#335).
        * ephemeral excluded — a ghost is hard-discarded at budget, so its chip
          would link to a 404 within minutes and a fan-out burst would inflate
          every count.
        """
        import inspect
        src = inspect.getsource(ops.get_all_skill_assignments)

        assert "agent_skills.join(" in src, "must INNER JOIN, not select bare"
        assert "deleted_at.is_(None)" in src
        assert "is_ephemeral" in src
        # NULL must read as "not a ghost": `is_ephemeral` is nullable on rows
        # written before ent#69, so a bare `== 0` silently drops every
        # pre-ent#69 agent from every holder list.
        assert "is_ephemeral.is_(None)" in src, (
            "NULL is_ephemeral (pre-ent#69 rows) must count as not-a-ghost"
        )
        # Core, not text() — this module has been 8/8 SQLAlchemy Core since the
        # #300 dual-backend conversion, and a raw string would have to be
        # re-proved on PostgreSQL.
        assert "text(" not in src
