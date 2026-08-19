"""ent#386 — assigning and unassigning a skill from the Library.

The read half (ent#384) answers "who holds this skill?". This is the write
half, and the thing it must not get wrong is *who may be offered as a target*.

Holders and assign targets are different sets, from different gates: the holder
list is owned ∪ shared (`GET /api/skills/assignments`), while the write routes
take `get_owned_agent_by_name`, which is owner-or-admin. A shared agent is
therefore a legitimate holder that this caller may not modify. Offer it in the
dropdown and the assign 404s; show it an unassign control and the control exists
only to fail.

That predicate is computed server-side on purpose. The alternative the issue
floated — deriving it client-side from `GET /api/agents` — needs an ownership
field that response does not carry, and would put a second copy of an
authorization rule in the browser, free to drift from the one the write route
actually enforces.
"""
from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db_harness import db_backend, seed_agent, seed_user  # noqa: E402,F401

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


_ROWS = [
    {"skill_name": "research", "agent_name": "scout", "display_label": None},
    {"skill_name": "writing", "agent_name": "scribe", "display_label": "Scribe"},
]

# `scout` is bob's; `scribe` is carol's but shared with bob; `sage` is carol's.
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


def _wire(monkeypatch, mod, assignable):
    monkeypatch.setattr(mod.db, "get_all_skill_assignments", lambda: list(_ROWS))
    monkeypatch.setattr(
        mod.db, "get_all_agent_metadata", lambda email="": dict(_METADATA)
    )
    seen = {}

    def _assignable(owner_username):
        seen["owner_username"] = owner_username
        return list(assignable)

    monkeypatch.setattr(mod.db, "get_assignable_agents", _assignable)
    return seen


# ---------------------------------------------------------------------------
# The endpoint carries the assign targets
# ---------------------------------------------------------------------------

def test_targets_ride_along_on_the_existing_read(router_mod, monkeypatch):
    """One read, not a second round-trip and not one call per skill block."""
    _wire(monkeypatch, router_mod, [{"agent_name": "scout", "display_label": None}])

    res = _call(router_mod, _User())

    assert [a.name for a in res.assignable_agents] == ["scout"]


def test_admin_is_queried_unfiltered(router_mod, monkeypatch):
    """`None` = no ownership filter. Passing the admin's own username instead
    would silently narrow the list to the agents that admin happens to own."""
    seen = _wire(monkeypatch, router_mod, [])

    _call(router_mod, _Admin())

    assert seen["owner_username"] is None


def test_non_admin_is_queried_by_their_own_username(router_mod, monkeypatch):
    seen = _wire(monkeypatch, router_mod, [])

    _call(router_mod, _User())

    assert seen["owner_username"] == "bob"


def test_a_shared_agent_is_a_holder_but_not_a_target(router_mod, monkeypatch):
    """The core asymmetry. `scribe` is shared with bob, so it shows as holding
    `writing` — but the write route is owner-or-admin, so it is not offered."""
    _wire(monkeypatch, router_mod, [{"agent_name": "scout", "display_label": None}])

    res = _call(router_mod, _User())

    assert [a.name for a in res.assignments["writing"]] == ["scribe"]
    assert "scribe" not in {a.name for a in res.assignable_agents}


def test_response_model_lets_nothing_extra_out(router_mod, monkeypatch):
    """The `response_model` is an allow-list, not documentation (ent#334).

    The db row is free to carry more columns later; only name and label may
    reach the wire.
    """
    _wire(
        monkeypatch, router_mod,
        [{"agent_name": "scout", "display_label": None, "owner_email": "leak@example.com"}],
    )

    res = _call(router_mod, _User())

    assert set(res.assignable_agents[0].model_dump()) == {"name", "display_label"}


# ---------------------------------------------------------------------------
# The query itself
# ---------------------------------------------------------------------------

@pytest.fixture
def ops():
    try:
        from db.skills import SkillsOperations
    except ImportError:  # pragma: no cover - backend venv required
        pytest.skip("backend venv required")
    return SkillsOperations()


def _seed_fleet():
    seed_user(1, "bob")
    seed_user(2, "carol")
    seed_agent("scout", owner_id=1)
    seed_agent("sage", owner_id=2)


def test_owner_filter_returns_only_the_callers_agents(db_backend, ops):
    _seed_fleet()

    names = [r["agent_name"] for r in ops.get_assignable_agents("bob")]

    assert names == ["scout"]


def test_admin_none_returns_the_whole_fleet(db_backend, ops):
    _seed_fleet()

    names = sorted(r["agent_name"] for r in ops.get_assignable_agents(None))

    assert names == ["sage", "scout"]


def test_an_owner_with_nothing_gets_an_empty_list_not_the_fleet(db_backend, ops):
    """The tri-state trap, at the db layer: "owns nothing" must not read as
    "admin". A falsy check on the username would return everything."""
    _seed_fleet()
    seed_user(3, "dave")

    assert ops.get_assignable_agents("dave") == []


def test_ghosts_are_never_offered(db_backend, ops):
    """A ghost is hard-discarded at budget, so a skill assigned to one stops
    meaning anything within minutes — and the ent#384 holder list already
    excludes them, so offering one produces an assignment that never appears."""
    _seed_fleet()
    seed_agent("ghost-a1b2", owner_id=1)
    from db.engine import get_engine
    from sqlalchemy import text
    with get_engine().begin() as conn:
        conn.execute(
            text("UPDATE agent_ownership SET is_ephemeral = 1 WHERE agent_name = 'ghost-a1b2'")
        )

    assert [r["agent_name"] for r in ops.get_assignable_agents("bob")] == ["scout"]


def test_soft_deleted_agents_are_never_offered(db_backend, ops):
    _seed_fleet()
    from db.engine import get_engine
    from sqlalchemy import text
    with get_engine().begin() as conn:
        conn.execute(
            text("UPDATE agent_ownership SET deleted_at = '2026-08-01T00:00:00Z' "
                 "WHERE agent_name = 'scout'")
        )

    assert ops.get_assignable_agents("bob") == []


def test_the_two_lists_share_their_exclusions(db_backend, ops):
    """Holder list and target list must exclude the same things.

    If they drift, the dropdown offers an agent the holder chips can never
    show: you assign the skill and, as far as the page is concerned, nothing
    happened.
    """
    _seed_fleet()
    from db.engine import get_engine
    from sqlalchemy import text
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE agent_ownership SET is_ephemeral = 1 WHERE agent_name = 'sage'"))
        conn.execute(
            text("INSERT INTO agent_skills (agent_name, skill_name, assigned_at, assigned_by) "
                 "VALUES ('sage', 'research', '2026-01-01T00:00:00Z', 'carol')")
        )

    holders = {r["agent_name"] for r in ops.get_all_skill_assignments()}
    targets = {r["agent_name"] for r in ops.get_assignable_agents(None)}

    assert "sage" not in holders
    assert "sage" not in targets


# ---------------------------------------------------------------------------
# No second write path
# ---------------------------------------------------------------------------

def test_no_skill_keyed_write_route_was_added():
    """ent#182: one skill model. The Library writes through the SAME per-agent
    routes the agent's Skills tab uses, which is where the owner gate lives — a
    second write path is a second place for that gate to drift.
    """
    source = (_BACKEND / "routers" / "skills.py").read_text()
    # Every mutating route that names a skill. Library/source management
    # (`/skills/library/sync`, `/skills/sources/...`) is a different concern and
    # legitimately lives here — what must not appear is an ASSIGNMENT writer
    # keyed by skill rather than by agent.
    mutating = re.findall(r'@router\.(?:post|delete)\("([^"]+)"', source)
    assignment_writes = [r for r in mutating if "{skill_name}" in r]

    assert sorted(assignment_writes) == [
        "/agents/{agent_name}/skills/{skill_name}",
        "/agents/{agent_name}/skills/{skill_name}",
    ]
