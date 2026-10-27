"""trinity-enterprise#719 — a room's moderator acts take a person.

Closing a room, adding an agent to it and removing one are the room's
moderator acts (`shared_sessions.service._require_moderator`). A platform
principal reaches them on two branches: the platform-admin bypass, and a `user`
participant row whose role is `moderator` (a room's creator is seated that
way). The rule on both: a platform principal moderates only as a PERSON
(`dependencies.is_person_principal` — a JWT session or the person's own
`user`-scoped key). A Workspace client keeps its own moderator branch, and an
agent stays refused, as before (ent#220).

Everything goes through the real rooms router and its real door,
`get_room_principal`: signed platform JWTs, a real Workspace session token,
and MCP keys whose row lookup is stubbed so the scope -> principal mapping in
`resolve_platform_user_unfloored` runs for real. The room tables are real
SQLite. Two principal shapes cannot be presented as a credential, so they are
handed to the router through a dependency override of the door: a loopback
principal carrying a vouched source agent, and a principal with no `mcp_scope`
at all. A real, signed loopback JWT with no source agent goes through the real
entry fence with no override. The Workspace cases use a real session token
through the real door, but stub the portal's block check and roster lookup.

The room tables come from their canonical DDL (`db.schema`), applied here
rather than through a bare `from conftest import ...`, which binds whichever
`conftest` module was imported last and so depends on test order.

Sync throughout (`tests/unit/pytest.ini` runs pytest-asyncio in strict mode).
"""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

ADMIN_EMAIL = "admin@example.com"
CAROL_EMAIL = "carol@example.com"
CLIENT_EMAIL = "client@example.com"
LOOPBACK_FENCE_DETAIL = "Event-loopback tokens may only dispatch a subscriber task"

OPS = ("close", "add", "remove")


# --------------------------------------------------------------------------- #
# Fixtures: real SQLite, the real router, real credentials
# --------------------------------------------------------------------------- #


ROOM_TABLES = (
    "enterprise_rooms",
    "enterprise_room_participants",
    "enterprise_room_messages",
)


def _seed(tmp_path, monkeypatch, users_rows, agents_rows) -> str:
    """Fresh SQLite: the OSS tables the room ACL reads, plus the room tables,
    seeded with `users_rows` (id, username, role, email) and `agents_rows`
    (agent_name, owner_id)."""
    db_file = tmp_path / "trinity-rooms.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))

    import db.connection as conn_mod

    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.engine import get_engine
    from db.tables import (
        agent_ownership,
        agent_sharing,
        operator_queue,
        schedule_executions,
        users,
    )
    from db.tables import metadata as m

    m.create_all(
        get_engine(),
        tables=[agent_ownership, agent_sharing, operator_queue, users, schedule_executions],
    )

    from db.schema import INDEXES, TABLES
    from sqlalchemy import insert, text

    markers = tuple(f"{name}(" for name in ROOM_TABLES)
    with get_engine().begin() as conn:
        for name in ROOM_TABLES:
            conn.execute(text(TABLES[name]))
        for stmt in INDEXES:
            if any(marker in stmt for marker in markers):
                conn.execute(text(stmt))

    with get_engine().begin() as conn:
        for user_id, username, role, email in users_rows:
            conn.execute(
                insert(users).values(
                    id=user_id,
                    username=username,
                    role=role,
                    email=email,
                    created_at="t",
                    updated_at="t",
                )
            )
        for name, owner_id in agents_rows:
            conn.execute(
                insert(agent_ownership).values(
                    agent_name=name, owner_id=owner_id, created_at="t"
                )
            )
    return str(db_file)


@pytest.fixture()
def rooms_db(tmp_path, monkeypatch):
    """`admin` (role admin) owns agent-a/b/c; `carol` (role creator) owns carol-a/b."""
    yield _seed(
        tmp_path,
        monkeypatch,
        [(1, "admin", "admin", ADMIN_EMAIL), (2, "carol", "creator", CAROL_EMAIL)],
        [("agent-a", 1), ("agent-b", 1), ("agent-c", 1), ("carol-a", 2), ("carol-b", 2)],
    )


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """A fresh install: its one admin, who signs in with a password and so has
    no email, and the two agents that admin made. No other user, no share, no
    permission edge, no room."""
    yield _seed(
        tmp_path,
        monkeypatch,
        [(1, "admin", "admin", None)],
        [("agent-a", 1), ("agent-b", 1)],
    )


# MCP keys, by bearer: (scope, agent_name, owner username, owner email). The
# row lookup is the only stub on the key path: the scope fences and the
# principal built from the row are the real code.
_KEY_ROWS = {
    "k-system": ("system", None, "admin", ADMIN_EMAIL),
    "k-admin-user": ("user", None, "admin", ADMIN_EMAIL),
    "k-carol-user": ("user", None, "carol", CAROL_EMAIL),
    "k-agent-a": ("agent", "agent-a", "admin", ADMIN_EMAIL),  # a member of the admin's room
    "k-agent-c": ("agent", "agent-c", "admin", ADMIN_EMAIL),  # an agent outside it
    "k-unknown": ("a-scope-no-one-has-shipped", None, "admin", ADMIN_EMAIL),
    "k-connector": ("connector", "agent-a", "admin", ADMIN_EMAIL),
    "k-ops": ("ops", None, "admin", ADMIN_EMAIL),
    "k-portal-delegate": ("portal_delegate", None, "admin", ADMIN_EMAIL),
    # A fresh install's admin has no email, so its key resolves by username.
    "k-system-no-email": ("system", None, "admin", None),
}


@pytest.fixture()
def keys(monkeypatch):
    import database

    def validate(token):
        row = _KEY_ROWS.get(token)
        if row is None:
            return None
        scope, agent_name, username, email = row
        return {
            "scope": scope,
            "agent_name": agent_name,
            "key_id": f"id-{token}",
            "key_name": token,
            "user_id": username,
            "user_email": email,
        }

    monkeypatch.setattr(database.db, "validate_mcp_api_key", validate)


def _jwt(username: str, **claims) -> str:
    from jose import jwt

    from config import ALGORITHM, SECRET_KEY

    return jwt.encode(
        {"sub": username, "exp": int(time.time()) + 300, **claims},
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def _workspace_session(email: str) -> str:
    from dependencies import create_portal_session_token

    return create_portal_session_token(email)


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _app() -> FastAPI:
    from shared_sessions.router import router

    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture()
def client(rooms_db, keys):
    app = _app()
    yield TestClient(app), app
    app.dependency_overrides.clear()


@pytest.fixture()
def fresh_client(fresh_db, keys):
    app = _app()
    yield TestClient(app), app
    app.dependency_overrides.clear()


def _create(client, token, agents, name="Room") -> str:
    r = client.post(
        "/api/rooms",
        json={"name": name, "agents": list(agents)},
        headers=_bearer(token),
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _act(client, op, room_id, token=None, *, add="agent-c", remove="agent-b"):
    headers = _bearer(token) if token else {}
    if op == "close":
        return client.post(
            f"/api/rooms/{room_id}/close", json={"reason": "done"}, headers=headers
        )
    if op == "add":
        return client.post(
            f"/api/rooms/{room_id}/participants",
            json={"agent_name": add},
            headers=headers,
        )
    return client.delete(f"/api/rooms/{room_id}/participants/{remove}", headers=headers)


def _active_agents(room_id) -> set[str]:
    from shared_sessions import db as rdb

    return {
        p["identity"]
        for p in rdb.list_participants(room_id)
        if p["kind"] == "agent" and not p.get("left_at")
    }


def _status(room_id) -> str:
    from shared_sessions import db as rdb

    return rdb.get_room(room_id)["status"]


# A caller acting as a platform account (participant kind `user`) that is not a
# person: the rename's shape, and the same body on every room it can see,
# whoever holds the moderator row there. An agent key acts as its agent and
# keeps `not_moderator`.
NOT_A_PERSON = {
    "detail": {"code": "not_a_person", "message": "Only a person can manage this room"}
}


def _assert_refused(response, room_id, agents_before, *, code, status=403):
    assert response.status_code == status, response.text
    assert response.json()["detail"]["code"] == code
    if code == "not_a_person":
        assert response.json() == NOT_A_PERSON
    assert _status(room_id) == "open", "a refused close still closed the room"
    assert _active_agents(room_id) == agents_before, "a refused act changed the roster"


def _admin_room(client) -> str:
    """A room the admin created over the router: the admin account holds its
    moderator row; agent-a and agent-b are members."""
    return _create(client, _jwt("admin"), ["agent-a", "agent-b"])


def _carol_room(client) -> str:
    """A room carol (a creator, not an admin) created: only carol is seated as a
    person, so an admin moderates it only through the admin branch."""
    return _create(client, _jwt("carol"), ["carol-a"])


# --------------------------------------------------------------------------- #
# The system key: refused on both platform branches
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("op", OPS)
def test_the_system_key_cannot_moderate_a_room_its_admin_owner_created(client, op):
    """The admin created the room and holds its moderator row; the system-scoped
    key resolves to that account. Neither the admin branch nor the moderator
    row admits it, so restoring EITHER branch turns this red."""
    http, _app = client
    room_id = _admin_room(http)
    before = _active_agents(room_id)

    _assert_refused(
        _act(http, op, room_id, "k-system"), room_id, before, code="not_a_person"
    )


@pytest.mark.parametrize("op", OPS)
def test_the_system_key_cannot_moderate_a_room_it_holds_no_seat_in(client, op):
    """Carol's room: no row for the admin account, so only the admin branch is
    in play. The admin account sees every room, so the key gets the person
    rule's 403 here, and the uniform 404 only on a room that does not exist."""
    http, _app = client
    room_id = _carol_room(http)
    before = _active_agents(room_id)

    _assert_refused(
        _act(http, op, room_id, "k-system", add="carol-b", remove="carol-a"),
        room_id,
        before,
        code="not_a_person",
    )
    ghost = _act(http, op, "room_doesnotexist", "k-system", add="carol-b", remove="carol-a")
    assert ghost.status_code == 404, ghost.text
    assert ghost.json()["detail"]["code"] == "room_not_found"


# --------------------------------------------------------------------------- #
# Every other machine principal
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("op", OPS)
def test_a_member_agent_cannot_moderate(client, op):
    """An agent participates by talking, never by managing the room (ent#220):
    its own key, on a room it is a member of, gets 403."""
    http, _app = client
    room_id = _admin_room(http)
    before = _active_agents(room_id)

    _assert_refused(
        _act(http, op, room_id, "k-agent-a"), room_id, before, code="not_moderator"
    )


@pytest.mark.parametrize("op", OPS)
def test_an_agent_outside_the_room_gets_the_uniform_404(client, op):
    """A sibling agent that is not a member learns nothing: the same 404 as a
    room that does not exist (membership is checked first, Invariant #8)."""
    http, _app = client
    room_id = _admin_room(http)
    before = _active_agents(room_id)

    real = _act(http, op, room_id, "k-agent-c")
    _assert_refused(real, room_id, before, status=404, code="room_not_found")
    ghost = _act(http, op, "room_doesnotexist", "k-agent-c")
    assert ghost.status_code == 404
    assert ghost.json() == real.json()


@pytest.mark.parametrize("op", OPS)
def test_an_unknown_key_scope_cannot_moderate(client, op):
    """The person rule is an allowlist: a scope no one has shipped is not a person."""
    http, _app = client
    room_id = _admin_room(http)
    before = _active_agents(room_id)

    _assert_refused(
        _act(http, op, room_id, "k-unknown"), room_id, before, code="not_a_person"
    )


@pytest.mark.parametrize("token", ["k-connector", "k-ops", "k-portal-delegate"])
@pytest.mark.parametrize("op", OPS)
def test_fenced_key_scopes_are_refused_at_the_door(client, op, token):
    """Connector, ops and portal-delegate keys are fenced at authentication, on
    the platform resolver and again on the Workspace fallback the rooms door
    tries next: the act never reaches the service."""
    http, _app = client
    room_id = _admin_room(http)
    before = _active_agents(room_id)

    r = _act(http, op, room_id, token)
    assert r.status_code == 403, r.text
    assert isinstance(r.json()["detail"], str)
    assert _status(room_id) == "open"
    assert _active_agents(room_id) == before


def _override_principal(app, principal):
    from shared_sessions.router import get_room_principal

    app.dependency_overrides[get_room_principal] = lambda: principal


@pytest.mark.parametrize("op", OPS)
def test_a_vouched_loopback_principal_cannot_moderate(client, op):
    """The event-loopback JWT resolves to the admin with no scope. Handed to the
    router as resolved (the entry fence is out of the picture), the service's
    own rule refuses it."""
    from models import User

    http, app = client
    room_id = _admin_room(http)
    before = _active_agents(room_id)
    _override_principal(
        app,
        User(
            id=1,
            username="admin",
            email=ADMIN_EMAIL,
            role="admin",
            mcp_scope=None,
            is_event_loopback=True,
            vouched_source_agent="agent-a",
        ),
    )

    _assert_refused(_act(http, op, room_id), room_id, before, code="not_a_person")


class _NoScopeAttribute:
    """An admin principal with NO `mcp_scope` attribute: the person rule fails
    closed on it (sentinel, never `getattr(..., None)`)."""

    id = 1
    username = "admin"
    email = ADMIN_EMAIL
    role = "admin"
    agent_name = None
    connector_agent = None
    portal_delegate = False


@pytest.mark.parametrize("op", OPS)
def test_a_principal_without_mcp_scope_cannot_moderate(client, op):
    http, app = client
    room_id = _admin_room(http)
    before = _active_agents(room_id)
    _override_principal(app, _NoScopeAttribute())

    _assert_refused(_act(http, op, room_id), room_id, before, code="not_a_person")


@pytest.mark.parametrize("op", OPS)
def test_an_unvouched_loopback_jwt_is_refused_by_the_entry_fence(client, op):
    """A real, signed loopback JWT with no source agent, through real
    authentication: the entry fence answers on every route but the subscriber
    task dispatch, the rooms door included."""
    from dependencies import EVENT_LOOPBACK_SCOPE

    http, _app = client
    room_id = _admin_room(http)
    before = _active_agents(room_id)

    r = _act(http, op, room_id, _jwt("admin", scope=EVENT_LOOPBACK_SCOPE))
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == LOOPBACK_FENCE_DETAIL
    assert _status(room_id) == "open"
    assert _active_agents(room_id) == before


def test_no_credential_stays_401(client):
    http, _app = client
    room_id = _admin_room(http)
    for op in OPS:
        assert _act(http, op, room_id).status_code == 401


# --------------------------------------------------------------------------- #
# The principals the rule keeps
# --------------------------------------------------------------------------- #


def _moderates(http, room_id, token, *, add, remove):
    r = _act(http, "add", room_id, token, add=add)
    assert r.status_code == 200 and r.json()["added"] is True, r.text
    assert add in _active_agents(room_id)
    r = _act(http, "remove", room_id, token, remove=remove)
    assert r.status_code == 200 and r.json()["removed"] is True, r.text
    assert remove not in _active_agents(room_id)
    r = _act(http, "close", room_id, token)
    assert r.status_code == 200 and r.json()["closed"] is True, r.text
    assert _status(room_id) == "closed"


@pytest.mark.parametrize("token", ["jwt", "k-admin-user"])
def test_a_person_admin_moderates_a_room_they_hold_no_seat_in(client, token):
    """The admin branch, for a person: the admin's session and the admin's own
    user-scoped key (PERSON admits a person's key)."""
    http, _app = client
    room_id = _carol_room(http)
    _moderates(
        http,
        room_id,
        _jwt("admin") if token == "jwt" else token,
        add="agent-c",
        remove="carol-a",
    )


@pytest.mark.parametrize("token", ["jwt", "k-carol-user"])
def test_the_rooms_creator_as_a_person_moderates_it(client, token):
    """The moderator-row branch, for a person who is not an admin."""
    http, _app = client
    room_id = _carol_room(http)
    _moderates(
        http,
        room_id,
        _jwt("carol") if token == "jwt" else token,
        add="carol-b",
        remove="carol-a",
    )


def test_a_workspace_moderator_keeps_its_branch(client, monkeypatch):
    """A Workspace client moderates the room it created (ent#361/ent#362). It
    is a verified email, not a platform principal, so the person rule — which
    reads platform credentials — is not its gate."""
    from client_portal import db as portal_db
    from client_portal import service as portal_service

    monkeypatch.setattr(portal_db, "is_client_blocked", lambda *_a, **_k: False)
    roster = {"agent-a", "agent-b", "agent-c"}
    monkeypatch.setattr(
        portal_service,
        "agent_on_roster",
        lambda agent_name, email, include_owned=False: (
            email == CLIENT_EMAIL and agent_name in roster
        ),
    )
    http, _app = client
    token = _workspace_session(CLIENT_EMAIL)
    room_id = _create(http, token, ["agent-a", "agent-b"])

    from shared_sessions import db as rdb

    seat = rdb.get_participant(room_id, "workspace_user", CLIENT_EMAIL)
    assert seat is not None and seat["role"] == "moderator"

    _moderates(http, room_id, token, add="agent-c", remove="agent-a")


# --------------------------------------------------------------------------- #
# The other shape: a fresh install (one admin with no email, its own agents)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("creator", ["admin session", "system key"])
def test_on_a_fresh_install_the_system_key_cannot_moderate_and_the_admin_can(
    fresh_client, creator
):
    """A fresh install has one person, its admin, who signs in with a password
    and so has no email: the system key resolves to that account by username,
    there being no email to look it up by. Whether the admin's session or the
    system key started the room, the admin account holds its only moderator
    seat. The key cannot close the room or change its roster; the admin,
    signed in, can, so no room is left without a moderator."""
    import database

    assert database.db.get_user_by_username("admin")["email"] is None

    http, _app = fresh_client
    creator_token = _jwt("admin") if creator == "admin session" else "k-system-no-email"
    room_id = _create(http, creator_token, ["agent-a"])

    from shared_sessions import db as rdb

    people = [p for p in rdb.list_participants(room_id) if p["kind"] != "agent"]
    assert [(p["kind"], p["identity"], p["role"]) for p in people] == [
        ("user", "admin", "moderator")
    ]

    before = _active_agents(room_id)
    for op in OPS:
        _assert_refused(
            _act(http, op, room_id, "k-system-no-email", add="agent-b", remove="agent-a"),
            room_id,
            before,
            code="not_a_person",
        )

    _moderates(http, room_id, _jwt("admin"), add="agent-b", remove="agent-a")


# --------------------------------------------------------------------------- #
# The same roster grant through a post: a mention recruits only for a person
# --------------------------------------------------------------------------- #


@pytest.fixture()
def no_wake(monkeypatch):
    """A woken agent's turn, stubbed: the recruit decision is under test, not the turn."""
    import types

    async def _execute_task(**_kw):
        return types.SimpleNamespace(
            status="success", response="ok", cost=0.0, error=None,
            execution_id="ex_stub", session_id="sess_stub",
        )

    import services.task_execution_service as tes

    monkeypatch.setattr(
        tes, "get_task_execution_service",
        lambda: types.SimpleNamespace(execute_task=_execute_task),
    )


def _edge(source: str, target: str) -> None:
    """An `agent_permissions` edge, so the agent key can reach `target` (ent#629)."""
    from db.engine import get_engine
    from db.tables import agent_permissions
    from db.tables import metadata as m
    from sqlalchemy import insert

    m.create_all(get_engine(), tables=[agent_permissions])
    with get_engine().begin() as conn:
        conn.execute(insert(agent_permissions).values(
            source_agent=source, target_agent=target, created_at="t", created_by="admin"))


def _post(http, room_id, token, content):
    return http.post(f"/api/rooms/{room_id}/messages", json={"content": content},
                     headers=_bearer(token))


@pytest.mark.parametrize("token", ["k-system", "k-agent-a"])
def test_a_machine_principals_mention_never_recruits(client, no_wake, token):
    """Mentioning an agent that is not in the room adds it (ent#361 AC#4), and
    only a person may recruit that way. The post itself still lands; the name
    stays plain text, as an unreachable one does. The agent key holds an edge
    to agent-c, so reach is not what refuses it."""
    _edge("agent-a", "agent-c")
    http, _app = client
    room_id = _admin_room(http)
    before = _active_agents(room_id)

    r = _post(http, room_id, token, "@agent-c please join")
    assert r.status_code == 200, r.text
    assert r.json()["mentions"] == [] and r.json()["woke"] == []
    assert _active_agents(room_id) == before


@pytest.mark.parametrize("token", ["jwt", "k-admin-user"])
def test_a_persons_mention_still_recruits(client, no_wake, token):
    http, _app = client
    room_id = _admin_room(http)

    r = _post(http, room_id, _jwt("admin") if token == "jwt" else token,
              "@agent-c please join")
    assert r.status_code == 200, r.text
    assert "agent-c" in _active_agents(room_id)
    assert r.json()["mentions"] == ["agent-c"]


def test_a_workspace_clients_mention_still_recruits(client, no_wake, monkeypatch):
    """The Workspace's "bring in another agent" path: a client's mention of an
    agent on its roster seats it."""
    from client_portal import db as portal_db
    from client_portal import service as portal_service

    monkeypatch.setattr(portal_db, "is_client_blocked", lambda *_a, **_k: False)
    roster = {"agent-a", "agent-b", "agent-c"}
    monkeypatch.setattr(
        portal_service, "agent_on_roster",
        lambda agent_name, email, include_owned=False: (
            email == CLIENT_EMAIL and agent_name in roster),
    )
    http, _app = client
    token = _workspace_session(CLIENT_EMAIL)
    room_id = _create(http, token, ["agent-a"])

    r = _post(http, room_id, token, "@agent-c please join")
    assert r.status_code == 200, r.text
    assert "agent-c" in _active_agents(room_id)


def test_an_agents_reply_never_recruits_even_when_a_person_woke_it(client, monkeypatch):
    """The engine posts a woken agent's reply under the waking person's
    principal, so the person rule alone would admit it: the reply is refused
    by its own check (`is_agent_reply`). The reply lands; the agent it names
    stays out, and is not woken."""
    import types

    import services.task_execution_service as tes

    woken = []

    async def _execute_task(**kw):
        woken.append(kw["agent_name"])
        return types.SimpleNamespace(
            status="success", response="@agent-c please join us", cost=0.0,
            error=None, execution_id=f"ex_{kw['agent_name']}", session_id="sess_stub",
        )

    monkeypatch.setattr(
        tes, "get_task_execution_service",
        lambda: types.SimpleNamespace(execute_task=_execute_task),
    )
    http, _app = client
    room_id = _admin_room(http)

    r = _post(http, room_id, _jwt("admin"), "@agent-a what do you think?")
    assert r.status_code == 200, r.text

    from shared_sessions import db as rdb

    replies = [m for m in rdb.get_messages(room_id, 0)
               if m["sender_kind"] == "agent" and m["sender_identity"] == "agent-a"]
    assert [m["content"] for m in replies] == ["@agent-c please join us"]
    assert "agent-c" not in _active_agents(room_id)
    assert woken == ["agent-a"]
