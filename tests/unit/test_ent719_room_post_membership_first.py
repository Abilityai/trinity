"""trinity-enterprise#719 — a room post checks membership before an idempotent replay.

`POST /api/rooms/{room_id}/messages` accepts an `Idempotency-Key` (Invariant #18):
repeating a key answers with the first post's stored result (a replay), or with
409 while that post is still being processed. The rule this file pins is that
membership is decided FIRST. A caller who is not in the room gets the uniform 404
on every answer the route gives to a well-formed post (a first post, a stored
replay and the in-flight 409 alike), the same response a room that does not exist
gets (Invariant #8), and claims no key in the room. A member's replay and a member's
409 are unchanged. A malformed body is refused by request validation before the
route runs, with the same 422 whether the caller is a member, a non-member, or
posting to a room that does not exist.

An agent key resolves to its owner and carries the owner's role, and on a default
install the owner is the admin. The service's admin view is for a platform user
(participant kind `user`), not for an agent key, so an admin-owned agent that holds
no seat is a non-member like any other. The system key carries no agent name, so
it is kind `user` with its admin owner's role, and that view admits it to every
room; its answer on this route is pinned as today's behaviour, not ruled here.

Everything goes through the real rooms router (`shared_sessions.router.router`),
the real rooms service and the real idempotency service, over a temp SQLite DB.
The principals are the resolved shapes `get_room_principal` hands the route, with
every scope field set, and that dependency is overridden. The loopback case sends
a real, signed event-loopback JWT through real authentication instead.

Edge case, the clock 30 days ahead: the clock is pinned at a fixed instant, the
member's first post stores its key, and then the clock moves 30 days, past the
24-hour key window. A non-member is still refused, and the member's repeat is a
NEW post, not a replay. The room is created with `ttl_hours=0` (no expiry) so it
is still open by then.

Sync throughout (`tests/unit/pytest.ini` runs pytest-asyncio in strict mode).
"""
from __future__ import annotations

import sys
import time
import types
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

import db.idempotency as idem_db
import shared_sessions.router as rooms_router
from models import User
from shared_sessions import db as rooms_db
from shared_sessions import service as rooms_service


_DEPS = rooms_router.resolve_platform_user_unfloored.__globals__  # `dependencies`
EVENT_LOOPBACK_SCOPE = _DEPS["EVENT_LOOPBACK_SCOPE"]
LOOPBACK_FENCE_DETAIL = "Event-loopback tokens may only dispatch a subscriber task"

PINNED = datetime(2026, 3, 2, 9, 0, 0, tzinfo=timezone.utc)
GHOST_ROOM = "room_doesnotexist0"
STATUS_POST = {"content": "status update, no mentions"}


def _iso(instant: datetime) -> str:
    return instant.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class _Clock(datetime):
    """`datetime` with `now()` answering a pinned instant the test moves."""

    instant = PINNED

    @classmethod
    def now(cls, tz=None):
        # Every clock read on this path is `datetime.now(timezone.utc)`. Trip on
        # any other shape rather than model local time.
        assert tz is not None, "the clock double models only aware UTC reads"
        return cls.instant.astimezone(tz)


# --------------------------------------------------------------------------- #
# Principals, in the shapes `get_room_principal` returns
# --------------------------------------------------------------------------- #

def _person(user_id: int, username: str, *, mcp_scope=None) -> User:
    """A person: a signed-in session (`mcp_scope=None`) or their own `user` key."""
    keyed = mcp_scope is not None
    return User(
        id=user_id, username=username, email=f"{username}@example.com", role="creator",
        agent_name=None, connector_agent=None, portal_delegate=False,
        mcp_scope=mcp_scope,
        mcp_key_id=f"mk-{username}" if keyed else None,
        mcp_key_name=f"{username} key" if keyed else None,
        vouched_source_agent=None, loopback_chain_depth=None, is_event_loopback=False,
    )


def _agent_key(agent_name: str, *, owner_id: int, owner: str,
               role: str = "creator") -> User:
    """An agent-scoped key: it resolves to its owner, carries the owner's role and
    the agent name."""
    return User(
        id=owner_id, username=owner, email=f"{owner}@example.com", role=role,
        agent_name=agent_name, connector_agent=None, portal_delegate=False,
        mcp_scope="agent", mcp_key_id=f"mk-{agent_name}", mcp_key_name=f"{agent_name} key",
        vouched_source_agent=None, loopback_chain_depth=None, is_event_loopback=False,
    )


def _system_key() -> User:
    """The system agent's key: `system` scope, no agent name, its admin owner."""
    return User(
        id=1, username="admin", email="admin@example.com", role="admin",
        agent_name=None, connector_agent=None, portal_delegate=False,
        mcp_scope="system", mcp_key_id="mk-system", mcp_key_name="system key",
        vouched_source_agent=None, loopback_chain_depth=None, is_event_loopback=False,
    )


def _workspace_client(email: str):
    return rooms_router.WorkspacePrincipal(email=email, is_platform=False)


# bob creates the room with agent-a and agent-b. agent-c is also bob's, agent-d
# is carol's and agent-e is the admin's; none of the three is seated.
MEMBERS = {
    "creator_session": lambda: _person(2, "bob"),
    "seated_agent_key": lambda: _agent_key("agent-a", owner_id=2, owner="bob"),
}
NON_MEMBERS = {
    "unseated_agent_key_same_owner": lambda: _agent_key("agent-c", owner_id=2, owner="bob"),
    "agent_key_other_owner": lambda: _agent_key("agent-d", owner_id=3, owner="carol"),
    "person_session": lambda: _person(3, "carol"),
    "person_user_key": lambda: _person(3, "carol", mcp_scope="user"),
    "workspace_client": lambda: _workspace_client("client@example.com"),
    # The issue's shape: the key carries its owner's admin role.
    "admin_owned_unseated_agent_key": lambda: _agent_key(
        "agent-e", owner_id=1, owner="admin", role="admin"),
}
# Pinned as today's answer, not a ruling: the admin view admits the system key.
ADMIN_VIEW = {
    "system_key": _system_key,
}


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #

@pytest.fixture
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-ent719-rooms.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.engine import get_engine
    from db.tables import (
        agent_ownership, idempotency_keys, metadata, operator_queue, schedule_executions, users)
    # `operator_queue` holds person tags, which every room read annotates.
    metadata.create_all(get_engine(), tables=[
        agent_ownership, users, schedule_executions, idempotency_keys, operator_queue])
    # The room tables come from their canonical DDL, not from a `conftest`
    # import: a bare `from conftest import ...` resolves through `sys.path`
    # and breaks when another file puts a different directory first.
    from db.schema import INDEXES, TABLES
    from sqlalchemy import text
    room_tables = ("enterprise_rooms", "enterprise_room_participants", "enterprise_room_messages")
    markers = tuple(f"{name}(" for name in room_tables)
    with get_engine().begin() as conn:
        for name in room_tables:
            conn.execute(text(TABLES[name]))
        for stmt in INDEXES:
            if any(marker in stmt for marker in markers):
                conn.execute(text(stmt))
    with get_engine().begin() as conn:
        for uid, name, role in ((1, "admin", "admin"), (2, "bob", "creator"),
                                (3, "carol", "creator")):
            conn.execute(insert(users).values(
                id=uid, username=name, role=role, email=f"{name}@example.com",
                created_at=_iso(PINNED), updated_at=_iso(PINNED)))
        for agent, owner_id in (("agent-a", 2), ("agent-b", 2), ("agent-c", 2), ("agent-d", 3),
                                ("agent-e", 1)):
            conn.execute(insert(agent_ownership).values(
                agent_name=agent, owner_id=owner_id, created_at=_iso(PINNED)))

    # One pinned clock for every read on this path: the key window
    # (`db.idempotency` via `utils.helpers`) and the room service.
    helpers = sys.modules[idem_db.utc_now_iso.__module__]
    assert idem_db.iso_cutoff.__globals__ is vars(helpers)
    _Clock.instant = PINNED
    monkeypatch.setattr(helpers, "datetime", _Clock)
    monkeypatch.setattr(rooms_service, "datetime", _Clock)
    # Every claim the real idempotency layer receives, so a test can show a
    # request never reached it (not only that nothing is left behind).
    from database import db as platform_db
    claims = []
    real_claim = platform_db.idempotency_claim

    def _recording_claim(scope, key, *args, **kwargs):
        claims.append((scope, key))
        return real_claim(scope, key, *args, **kwargs)

    monkeypatch.setattr(platform_db, "idempotency_claim", _recording_claim)
    # The WS fan-out is out of scope here and would import the whole app.
    monkeypatch.setattr(rooms_service, "_broadcast", lambda *_a, **_k: None)

    # Room creation is setup, not the subject: its agent-access check is stubbed
    # for this call only, so no request below runs with it.
    import dependencies
    with monkeypatch.context() as m:
        m.setattr(dependencies, "assert_agent_access", lambda *_a, **_k: None)
        room = rooms_service.create_room(
            MEMBERS["creator_session"](), "Ops", ["agent-a", "agent-b"], ttl_hours=0)
    assert room["expires_at"] is None

    app = FastAPI()
    app.include_router(rooms_router.router)
    holder = {}
    app.dependency_overrides[rooms_router.get_room_principal] = lambda: holder["principal"]
    yield types.SimpleNamespace(
        app=app, client=TestClient(app), holder=holder, room_id=room["id"],
        engine=get_engine(), keys=idempotency_keys, claims=claims)
    app.dependency_overrides.clear()


def _post(env, principal, room_id=None, key=None):
    env.holder["principal"] = principal
    headers = {"Idempotency-Key": key} if key is not None else {}
    return env.client.post(
        f"/api/rooms/{room_id or env.room_id}/messages", json=STATUS_POST, headers=headers)


def _messages(env):
    return [m for m in rooms_db.get_messages(env.room_id) if m["kind"] == "message"]


def _key_rows(env, key=None):
    stmt = select(env.keys.c.scope, env.keys.c.idempotency_key, env.keys.c.status,
                  env.keys.c.response_snapshot, env.keys.c.created_at)
    if key is not None:
        stmt = stmt.where(env.keys.c.idempotency_key == key)
    with env.engine.connect() as conn:
        return [dict(r) for r in conn.execute(stmt).mappings()]


def _store_a_member_post(env, key):
    """bob posts with `key`; returns (response json, the stored key row)."""
    first = _post(env, MEMBERS["creator_session"](), key=key)
    assert first.status_code == 200, first.text
    assert "replayed" not in first.json()
    (row,) = _key_rows(env, key)
    assert row["status"] == idem_db.STATE_COMPLETED
    return first.json(), row


def _hold_in_flight(env, key):
    """A post with `key` that is still being processed: a live claim on the
    room's scope (read from a real completed post, not spelled here)."""
    _, row = _store_a_member_post(env, "scope-probe")
    from database import db as platform_db
    assert platform_db.idempotency_claim(row["scope"], key)["state"] == idem_db.STATE_NEW
    (held,) = _key_rows(env, key)
    assert held["status"] == idem_db.STATE_IN_FLIGHT
    return held


def _ghost_404(env, principal, key):
    """What the same caller gets from a room that does not exist."""
    response = _post(env, principal, room_id=GHOST_ROOM, key=key)
    assert response.status_code == 404, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# A non-member: the uniform 404 on every answer
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("who", list(NON_MEMBERS))
def test_a_non_member_never_gets_a_stored_replay(env, who):
    _first, row = _store_a_member_post(env, "k-stored")
    before, seen = _messages(env), len(env.claims)

    response = _post(env, NON_MEMBERS[who](), key="k-stored")

    assert response.status_code == 404, response.text
    assert response.json() == _ghost_404(env, NON_MEMBERS[who](), "k-stored")
    assert env.claims[seen:] == []  # neither request reached the key claim
    assert response.json() == {"detail": {"code": "room_not_found", "message": "Room not found"}}
    assert _messages(env) == before
    assert _key_rows(env, "k-stored") == [row]  # the member's stored post is untouched


@pytest.mark.parametrize("who", list(NON_MEMBERS))
def test_a_non_member_is_not_shown_the_in_flight_409(env, who):
    held = _hold_in_flight(env, "k-busy")
    before, seen = _messages(env), len(env.claims)

    response = _post(env, NON_MEMBERS[who](), key="k-busy")

    assert response.status_code == 404, response.text
    assert response.json() == _ghost_404(env, NON_MEMBERS[who](), "k-busy")
    assert env.claims[seen:] == []
    assert _messages(env) == before
    assert _key_rows(env, "k-busy") == [held]  # the live claim is neither shown nor released


@pytest.mark.parametrize("who", list(NON_MEMBERS))
def test_a_non_member_gets_the_same_404_with_a_fresh_key_or_none(env, who):
    ghost = _ghost_404(env, NON_MEMBERS[who](), "k-fresh")

    with_key = _post(env, NON_MEMBERS[who](), key="k-fresh")
    without_key = _post(env, NON_MEMBERS[who]())

    assert (with_key.status_code, with_key.json()) == (404, ghost)
    assert (without_key.status_code, without_key.json()) == (404, ghost)
    assert _messages(env) == []
    assert env.claims == []  # no request reached the key claim...
    assert _key_rows(env) == []  # ...and none left a row behind

    # ...so the key is still the member's to use: a first post, not a 409 or a replay.
    member = _post(env, MEMBERS["creator_session"](), key="k-fresh")
    assert member.status_code == 200, member.text
    assert "replayed" not in member.json()


@pytest.mark.parametrize("who", list(NON_MEMBERS))
def test_a_malformed_post_gets_the_same_422_member_or_not(env, who):
    """Request validation runs before the route, so a malformed body is refused
    with 422 before membership is decided. That answer is the same for a member,
    a non-member and a room that does not exist, and claims no key."""
    _store_a_member_post(env, "k-bad")
    before, seen = _messages(env), len(env.claims)

    def bad(principal, room_id=None):
        env.holder["principal"] = principal
        return env.client.post(f"/api/rooms/{room_id or env.room_id}/messages",
                               json={"content": ""}, headers={"Idempotency-Key": "k-bad"})

    outsider = bad(NON_MEMBERS[who]())
    ghost = bad(NON_MEMBERS[who](), room_id=GHOST_ROOM)
    member = bad(MEMBERS["creator_session"]())

    assert outsider.status_code == ghost.status_code == member.status_code == 422
    assert outsider.json() == ghost.json() == member.json()
    assert env.claims[seen:] == []
    assert _messages(env) == before


# --------------------------------------------------------------------------- #
# A member: unchanged
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("who", list(MEMBERS))
def test_a_member_replay_is_unchanged(env, who):
    first = _post(env, MEMBERS[who](), key="k-mine")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["room_id"] == env.room_id and body["seq"] and body["mentions"] == []

    again = _post(env, MEMBERS[who](), key="k-mine")

    assert again.status_code == 200, again.text
    assert again.json() == {**body, "replayed": True}
    assert [m["seq"] for m in _messages(env)] == [body["seq"]]  # one post, not two


@pytest.mark.parametrize("who", list(MEMBERS))
def test_a_member_is_shown_the_in_flight_409(env, who):
    held = _hold_in_flight(env, "k-busy")
    before = _messages(env)

    response = _post(env, MEMBERS[who](), key="k-busy")

    assert response.status_code == 409, response.text
    assert response.json() == {"detail": {
        "error": "request_in_progress",
        "message": "A post with this Idempotency-Key is still being processed.",
        "execution_id": None,
    }}
    assert _messages(env) == before
    assert _key_rows(env, "k-busy") == [held]


@pytest.mark.parametrize("who", list(ADMIN_VIEW))
def test_the_admin_view_still_admits_the_system_key(env, who):
    """Today's answer, pinned: the system key holds no seat in the room, posts as
    its admin owner, and its own repeat is a replay of that post."""
    first = _post(env, ADMIN_VIEW[who](), key="k-system")
    assert first.status_code == 200, first.text
    body = first.json()
    assert "replayed" not in body

    again = _post(env, ADMIN_VIEW[who](), key="k-system")

    assert again.status_code == 200, again.text
    assert again.json() == {**body, "replayed": True}
    (posted,) = _messages(env)
    assert (posted["seq"], posted["sender_kind"], posted["sender_identity"]) == (
        body["seq"], "user", "admin")


# --------------------------------------------------------------------------- #
# Edge case: the clock 30 days ahead
# --------------------------------------------------------------------------- #

def test_thirty_days_on_a_non_member_is_still_refused_and_a_member_post_is_new(env):
    first, row = _store_a_member_post(env, "k-month")
    assert row["created_at"] == _iso(PINNED)

    later = PINNED + timedelta(days=30)
    _Clock.instant = later

    seen = len(env.claims)
    for who in NON_MEMBERS:
        response = _post(env, NON_MEMBERS[who](), key="k-month")
        assert response.status_code == 404, (who, response.text)
        assert response.json() == _ghost_404(env, NON_MEMBERS[who](), "k-month")
    assert env.claims[seen:] == []
    assert _key_rows(env, "k-month") == [row]  # expired, and no non-member re-claimed it

    again = _post(env, MEMBERS["creator_session"](), key="k-month")

    assert again.status_code == 200, again.text
    assert "replayed" not in again.json()
    assert again.json()["seq"] > first["seq"]
    newest = _messages(env)[-1]
    assert (newest["seq"], newest["created_at"]) == (again.json()["seq"], _iso(later))
    (reclaimed,) = _key_rows(env, "k-month")
    assert (reclaimed["status"], reclaimed["created_at"]) == (idem_db.STATE_COMPLETED, _iso(later))


# --------------------------------------------------------------------------- #
# The event loopback never reaches the route
# --------------------------------------------------------------------------- #

def test_a_real_loopback_jwt_is_refused_before_the_room_is_reached(env):
    """A real, signed loopback JWT with no source agent, through real
    authentication (no override): the entry fence refuses it on this route."""
    from config import ALGORITHM, SECRET_KEY
    from jose import jwt

    _first, row = _store_a_member_post(env, "k-loop")
    before = _messages(env)
    env.app.dependency_overrides.clear()
    token = jwt.encode(
        {"sub": "bob", "scope": EVENT_LOOPBACK_SCOPE, "exp": int(time.time()) + 300},
        SECRET_KEY, algorithm=ALGORITHM)

    response = env.client.post(
        f"/api/rooms/{env.room_id}/messages", json=STATUS_POST,
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "k-loop"})

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == LOOPBACK_FENCE_DETAIL
    assert _messages(env) == before
    assert _key_rows(env, "k-loop") == [row]
