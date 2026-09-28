"""trinity-enterprise#713 — `GET /api/events` is scoped to the caller's accessible agents.

Two halves, both against the real code and the real (per-process tmp SQLite) schema:

* DB half — ``db.list_agent_events(agent_names=...)`` runs the production query:
  ``None`` is unrestricted, ``[]`` returns before any SQL, a list becomes
  ``source_agent IN (...)`` with ``LIMIT`` applied after it.
* Router half — the real ``list_all_events`` handler, called with real
  ``models.User`` principals, over users / ownership / sharing rows written by
  the real db writers. The access boundary (``db.get_accessible_agent_names``,
  ``assert_agent_access``) is never stubbed.

Isolation in the shared per-process DB: every test uses fresh uuid-suffixed
user / agent names AND its own ``event_type``, passed on every query, so rows
written by other tests can neither crowd a result nor make a value ambient.
Seeded rows get explicit, strictly increasing far-future ``created_at`` values
so ordering is deterministic and nothing written "now" can outrank them.

The principal fences in ``get_current_user`` (connector / portal / ops /
ephemeral) run before this handler and are covered by their own suites.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

_BACKEND = str(Path(__file__).resolve().parents[2] / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import dependencies  # noqa: E402
import routers.event_subscriptions as events_router  # noqa: E402
from db.event_subscriptions import EventSubscriptionOperations  # noqa: E402
from db_models import UserCreate  # noqa: E402
from models import User as Principal  # noqa: E402

pytestmark = pytest.mark.unit

# The module the DB method actually resolves its engine / table from. Patching
# or reading any other module attribute would detach (module-identity trap).
_EVENTS_GLOBALS = EventSubscriptionOperations.list_events.__globals__


@pytest.fixture(autouse=True)
def _real_database():
    """Fail loud (never skip) if a sibling's stub replaced the real DB object."""
    assert type(events_router.db).__name__ == "DatabaseManager", type(events_router.db)
    assert type(dependencies.db).__name__ == "DatabaseManager", type(dependencies.db)
    return events_router.db


def _uid() -> str:
    return uuid.uuid4().hex[:10]


def _event_type() -> str:
    # Valid dot-separated identifier; unique per test.
    return f"evt_{_uid()}.scope"


def _principal(prefix: str, role: str = "user") -> Principal:
    db = events_router.db
    username = f"{prefix}-{_uid()}"
    email = f"{username}@example.com"
    row = db.create_user(UserCreate(username=username, email=email, role=role))
    return Principal(id=row["id"], username=username, email=email, role=role, mcp_scope=None)


def _agent(prefix: str, owner: Principal) -> str:
    name = f"{prefix}-{_uid()}"
    assert events_router.db.register_agent_owner(name, owner.username)
    return name


def _seed_events(source_agent: str, event_type: str, n: int) -> list[str]:
    ids = []
    for i in range(n):
        ev = events_router.db.create_agent_event(source_agent, event_type, {"i": i})
        ids.append(ev.id)
    return ids


def _stamp_in_order(event_ids: list[str]) -> None:
    """Rewrite created_at to strictly increasing far-future values (oldest first)."""
    t = _EVENTS_GLOBALS["agent_events"]
    with _EVENTS_GLOBALS["get_engine"]().begin() as conn:
        for i, event_id in enumerate(event_ids):
            conn.execute(
                t.update()
                .where(t.c.id == event_id)
                .values(created_at=f"2999-01-01T00:{i // 60:02d}:{i % 60:02d}Z")
            )


async def _list(user: Principal, event_type: str, source_agent=None, limit: int = 500):
    # Every argument explicit: the defaults are FastAPI Query(...) objects.
    return await events_router.list_all_events(
        source_agent=source_agent,
        event_type=event_type,
        limit=limit,
        current_user=user,
    )


# ---------------------------------------------------------------------------
# DB half
# ---------------------------------------------------------------------------


@pytest.fixture
def db_rows():
    owner = _principal("dbowner")
    mine = _agent("mine", owner)
    unshared = _agent("unshared", owner)
    event_type = _event_type()
    mine_ids = _seed_events(mine, event_type, 3)
    unshared_ids = _seed_events(unshared, event_type, 5)
    # 3 `mine` rows older, 5 `unshared` rows newer.
    _stamp_in_order(mine_ids + unshared_ids)
    return {"mine": mine, "unshared": unshared, "event_type": event_type, "mine_ids": mine_ids}


def test_ent713_db_agent_names_filter_applies_before_limit(db_rows):
    events = events_router.db.list_agent_events(
        event_type=db_rows["event_type"], agent_names=[db_rows["mine"]], limit=2
    )
    # The two newest rows overall belong to `unshared`; a post-filter would return 0.
    assert [e.id for e in events] == list(reversed(db_rows["mine_ids"]))[:2]
    assert {e.source_agent for e in events} == {db_rows["mine"]}


def test_ent713_db_empty_agent_names_returns_empty_without_query(db_rows, monkeypatch):
    calls = []
    real_get_engine = _EVENTS_GLOBALS["get_engine"]

    def spy():
        calls.append(1)
        return real_get_engine()

    monkeypatch.setitem(_EVENTS_GLOBALS, "get_engine", spy)
    events = events_router.db.list_agent_events(
        event_type=db_rows["event_type"], agent_names=[], limit=50
    )
    assert events == []
    assert calls == []


def test_ent713_db_none_agent_names_is_unrestricted(db_rows):
    events = events_router.db.list_agent_events(
        event_type=db_rows["event_type"], agent_names=None, limit=50
    )
    assert {e.source_agent for e in events} == {db_rows["mine"], db_rows["unshared"]}
    assert len(events) == 8


# ---------------------------------------------------------------------------
# Router half
# ---------------------------------------------------------------------------


@pytest.fixture
def fleet():
    db = events_router.db
    alice = _principal("alice")
    bob = _principal("bob")
    admin = _principal("admin", role="admin")

    mine = _agent("mine", alice)
    shared = _agent("shared", bob)
    unshared = _agent("unshared", bob)
    other = _agent("other", bob)
    ghost = _agent("ghost", bob)
    assert db.share_agent(shared, bob.username, alice.email)
    assert db.share_agent(ghost, bob.username, alice.email)
    assert db.delete_agent_ownership(ghost)  # soft delete; the share row stays

    event_type = _event_type()
    ids = []
    for agent in (mine, shared, unshared, other, ghost):
        ids += _seed_events(agent, event_type, 2)
    _stamp_in_order(ids)

    # Preconditions that keep the ghost case meaningful: the per-agent access
    # predicate still admits the stale share, while the roster excludes it.
    assert db.can_user_access_agent(alice.username, ghost)
    assert ghost not in db.get_accessible_agent_names(alice.email, is_admin=False)

    return {
        "alice": alice, "bob": bob, "admin": admin, "event_type": event_type,
        "mine": mine, "shared": shared, "unshared": unshared, "other": other, "ghost": ghost,
    }


@pytest.mark.asyncio
async def test_ent713_list_is_limited_to_owned_and_shared_agents(fleet):
    result = await _list(fleet["alice"], fleet["event_type"])
    assert {e.source_agent for e in result.events} == {fleet["mine"], fleet["shared"]}
    assert result.count == 4


@pytest.mark.asyncio
async def test_ent713_admin_list_is_unrestricted(fleet):
    result = await _list(fleet["admin"], fleet["event_type"])
    sources = {e.source_agent for e in result.events}
    assert {fleet["mine"], fleet["shared"], fleet["unshared"], fleet["other"]} <= sources
    assert fleet["unshared"] in sources


@pytest.mark.asyncio
async def test_ent713_empty_source_agent_keeps_the_roster(fleet):
    result = await _list(fleet["alice"], fleet["event_type"], source_agent="")
    assert {e.source_agent for e in result.events} == {fleet["mine"], fleet["shared"]}


@pytest.mark.asyncio
async def test_ent713_source_agent_within_roster_is_narrowed(fleet):
    result = await _list(fleet["alice"], fleet["event_type"], source_agent=fleet["shared"])
    assert {e.source_agent for e in result.events} == {fleet["shared"]}
    assert result.count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["unshared", "nonexistent", "ghost"])
async def test_ent713_source_agent_outside_roster_is_uniform_403(fleet, target):
    name = f"nonexistent-{_uid()}" if target == "nonexistent" else fleet[target]
    with pytest.raises(HTTPException) as exc:
        await _list(fleet["alice"], fleet["event_type"], source_agent=name)
    assert exc.value.status_code == 403
    assert exc.value.detail == "Access denied"


@pytest.mark.asyncio
async def test_ent713_admin_targeted_source_agent_is_allowed(fleet):
    result = await _list(fleet["admin"], fleet["event_type"], source_agent=fleet["unshared"])
    assert {e.source_agent for e in result.events} == {fleet["unshared"]}
