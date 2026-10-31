"""trinity-enterprise#719 — the room uses the route census lists as DELEGATED.

`GET /api/rooms`, `GET /api/rooms/{room_id}` and `PATCH /api/rooms/{room_id}`
take `get_room_principal` (a platform user, an agent key or a Workspace
client), so no census gate fits them. Agents read and post in rooms as
themselves by design (ent#169); what keeps a room to its members lives in
`shared_sessions/service.py`, and each DELEGATED entry names the service check
and the test below that proves it refuses:

* the room list is the caller's own memberships (`db.list_rooms_for_participant`);
* reading or renaming a room answers a non-member with a uniform 404
  (`_require_membership`), the same answer as for a room that does not exist;
* an agent key cannot rename a room: a member agent gets a named 403
  (`not_a_person`).

`POST /api/rooms` stays in the frozen baseline: creating a room also sets its
budget, and who may do that is ruled on its own. Its reach rule is pinned here
for that ruling: every agent named must be inside the creator's reach
(`_assert_can_reach_agent`: the access check plus the ent#629 reach), with one
uniform 403 for an unknown and an unreachable agent.

No runtime behaviour changes. Every principal is a real credential through real
authentication, with no dependency override: an agent's own MCP key, a person's
`user`-scoped key, a signed-in session JWT and a real event-loopback JWT. The
fleet has an admin owner (the worst case): A, the calling agent; B, which A
holds an edge to; C, a sibling with no edge (also used as a non-member agent
key); D, an agent A spawned with its current key; P, an agent A spawned under a
key it no longer holds; F, another user's agent. Every check reads the real
production schema, built once per module with `db_harness.bootstrap_schema` and
copied fresh for each test (SQLite).
"""
from __future__ import annotations

import importlib
import shutil
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _route_census as rc  # noqa: E402

_TESTS = str(Path(__file__).resolve().parents[1])
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)
from db_harness import bootstrap_schema, run, scalar, seed_agent, seed_user  # noqa: E402

pytestmark = pytest.mark.unit

OWNER, OWNER_EMAIL = "owner", "owner@example.com"
OTHER, OTHER_EMAIL = "other", "other@example.com"
A, B, C, D, F = "a-alpha", "a-beta", "a-gamma", "a-delta", "o-foreign"
P = "a-delta-prior"  # spawned by A under a key A no longer holds

CREATE = "shared_sessions/router.py::create_room"
S1C = (
    "shared_sessions/router.py::list_rooms",
    "shared_sessions/router.py::get_room",
    "shared_sessions/router.py::rename_room",
)
NOT_FOUND = {"detail": {"code": "room_not_found", "message": "Room not found"}}
LOOPBACK_FENCE_DETAIL = "Event-loopback tokens may only dispatch a subscriber task"
MISSING_ROOM = "room_0000000000000000"
UNKNOWN_AGENT = "no-such-agent"


def _not_accessible(agent: str) -> dict:
    """The one refusal `_assert_can_reach_agent` gives, unknown or unreachable."""
    return {"detail": {"code": "agent_not_accessible",
                       "message": f"You do not have access to agent {agent!r}"}}


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def _schema_template(tmp_path_factory):
    """The production schema, built ONCE per module with `db_harness`'s own
    builder, so a test starts from a byte copy instead of re-running every DDL
    statement (about ten seconds a test on a loaded laptop)."""
    from db.engine import dispose_engines

    template = tmp_path_factory.mktemp("ent719-rooms-schema") / "trinity.db"
    patch = pytest.MonkeyPatch()
    try:
        patch.setenv("TRINITY_DB_PATH", str(template))
        patch.delenv("DATABASE_URL", raising=False)
        dispose_engines()
        bootstrap_schema()
    finally:
        dispose_engines()
        patch.undo()
    return template


@pytest.fixture
def fresh_db(_schema_template, tmp_path, monkeypatch):
    """A fresh database per test: a copy of this module's template."""
    from db.engine import dispose_engines

    path = tmp_path / "trinity.db"
    for suffix in ("", "-wal", "-shm"):
        source = Path(str(_schema_template) + suffix)
        if source.exists():
            shutil.copyfile(source, str(path) + suffix)
    monkeypatch.setenv("TRINITY_DB_PATH", str(path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    dispose_engines()
    try:
        yield path
    finally:
        dispose_engines()


@pytest.fixture
def rooms(fresh_db, monkeypatch):
    """The fleet above with real keys, and the real rooms router."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    router_mod = importlib.import_module("shared_sessions.router")
    service = importlib.import_module("shared_sessions.service")
    # The namespace the room principal really authenticates through (the unit
    # conftest pops `dependencies` between tests, so a fresh import is not it).
    deps = router_mod.resolve_platform_user_unfloored.__globals__
    db = deps["db"]

    seed_user(1, OWNER, role="admin")
    seed_user(2, OTHER, role="user")
    run("UPDATE users SET email = :e WHERE id = 1", e=OWNER_EMAIL)
    run("UPDATE users SET email = :e WHERE id = 2", e=OTHER_EMAIL)
    for name in (A, B, C, D, P):
        seed_agent(name, owner_id=1)
    seed_agent(F, owner_id=2)
    db.add_agent_permission(A, B, created_by=OWNER)
    key_a = db.create_agent_mcp_api_key(A, OWNER)
    key_c = db.create_agent_mcp_api_key(C, OWNER)
    run("UPDATE agent_ownership SET spawned_by_agent = :a, spawned_by_key_id = :k "
        "WHERE agent_name = :d", a=A, k=key_a.id, d=D)
    run("UPDATE agent_ownership SET spawned_by_agent = :a, spawned_by_key_id = :k "
        "WHERE agent_name = :p", a=A, k="key-retired-before-this-one", p=P)

    from db_models import McpApiKeyCreate
    user_key = db.create_mcp_api_key(OWNER, McpApiKeyCreate(name="owner-scripts"))

    # A signed-in session asks Redis whether the token was revoked; the unit tier
    # has no Redis, so answer "not revoked" there.
    monkeypatch.setitem(deps, "is_token_revoked", lambda *_a, **_k: False)
    monkeypatch.setitem(deps, "session_predates_user_revoke", lambda *_a, **_k: False)
    session = deps["create_access_token"]({"sub": OWNER})

    broadcasts = []
    monkeypatch.setattr(service, "_broadcast", lambda event, payload: broadcasts.append(event))

    app = FastAPI()
    app.include_router(router_mod.router)
    client = TestClient(app, raise_server_exceptions=True)

    def as_(token):
        return {"Authorization": f"Bearer {token}"}

    return type("Rooms", (), {
        "db": db,
        "deps": deps,
        "client": client,
        "agent": as_(key_a.api_key),
        "outsider": as_(key_c.api_key),
        "user_key": as_(user_key.api_key),
        "session": as_(session),
        "as_": staticmethod(as_),
        "broadcasts": broadcasts,
    })()


def _path(key: str, room_id: str = "") -> tuple:
    """The census's own stored METHOD and path for the route, the room id
    filled in: the DELEGATED entry, or the baseline entry for room creation."""
    stored = rc.DELEGATED[key][0] if key in rc.DELEGATED else rc.load_baseline()[key]
    method, path = stored.split(" ", 1)
    return method, path.replace("{room_id}", room_id)


def _create(rooms, headers, agents, name="Ops"):
    method, url = _path(CREATE)
    return rooms.client.request(method, url, json={"name": name, "agents": list(agents)},
                                headers=headers)


def _room_count() -> int:
    return scalar("SELECT COUNT(*) FROM enterprise_rooms")


# --------------------------------------------------------------------------- #
# The census lists them, each with its service check and its test
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def classified():
    baseline = rc.load_baseline()
    classes, failures = rc.classify(rc.walk(), baseline)
    return baseline, classes, failures


@pytest.mark.parametrize("key", S1C)
def test_each_room_use_is_listed_delegated_and_not_in_the_baseline(classified, key):
    baseline, classes, failures = classified
    assert classes[key] == "delegated"
    assert key not in baseline
    assert not [m for _t, m in failures if key in m]
    _mp, service_check, test_id = rc.DELEGATED[key]
    assert service_check.startswith("shared_sessions.service.")
    assert test_id.startswith("tests/unit/test_ent719_room_uses_delegated.py::")


# --------------------------------------------------------------------------- #
# The named behavioural tests, one per DELEGATED entry
# --------------------------------------------------------------------------- #

def test_an_agent_lists_only_the_rooms_it_is_in(rooms):
    """list_rooms: an agent key sees its own memberships, never its owner's
    rooms; an agent in no room gets an empty list."""
    with_a = _create(rooms, rooms.session, [A, B], name="with-a").json()["id"]
    with_c = _create(rooms, rooms.session, [C], name="with-c").json()["id"]
    method, url = _path("shared_sessions/router.py::list_rooms")

    def listed(headers):
        response = rooms.client.request(method, url, headers=headers)
        assert response.status_code == 200, response.text
        return {r["id"] for r in response.json()["rooms"]}

    assert listed(rooms.agent) == {with_a}
    assert listed(rooms.outsider) == {with_c}
    assert listed(rooms.session) == {with_a, with_c}   # a person admin sees every room


def test_a_non_member_agent_gets_the_uniform_404_on_read(rooms):
    """get_room: a non-member agent's own key gets exactly the answer a missing
    room gives; a member agent reads the room."""
    room_id = _create(rooms, rooms.session, [A], name="members-only").json()["id"]
    method, url = _path("shared_sessions/router.py::get_room", room_id)
    _m, missing = _path("shared_sessions/router.py::get_room", MISSING_ROOM)

    refused = rooms.client.request(method, url, headers=rooms.outsider)
    assert (refused.status_code, refused.json()) == (404, NOT_FOUND)
    absent = rooms.client.request(method, missing, headers=rooms.outsider)
    assert (absent.status_code, absent.json()) == (404, NOT_FOUND)

    member = rooms.client.request(method, url, headers=rooms.agent)
    assert member.status_code == 200, member.text
    assert member.json()["id"] == room_id


def test_a_non_member_agent_gets_the_uniform_404_on_rename(rooms):
    """rename_room: a non-member agent key gets the missing-room 404; a member
    agent's key is refused by name; a person member renames; the title moves
    only for the person."""
    room_id = _create(rooms, rooms.session, [A], name="before").json()["id"]
    method, url = _path("shared_sessions/router.py::rename_room", room_id)
    _m, missing = _path("shared_sessions/router.py::rename_room", MISSING_ROOM)
    body = {"name": "after"}

    refused = rooms.client.request(method, url, json=body, headers=rooms.outsider)
    assert (refused.status_code, refused.json()) == (404, NOT_FOUND)
    absent = rooms.client.request(method, missing, json=body, headers=rooms.outsider)
    assert (absent.status_code, absent.json()) == (404, NOT_FOUND)

    member = rooms.client.request(method, url, json=body, headers=rooms.agent)
    assert member.status_code == 403
    assert member.json()["detail"]["code"] == "not_a_person"
    assert scalar("SELECT name FROM enterprise_rooms WHERE id = :i", i=room_id) == "before"
    assert rooms.broadcasts == []

    person = rooms.client.request(method, url, json=body, headers=rooms.session)
    assert person.status_code == 200, person.text
    assert scalar("SELECT name FROM enterprise_rooms WHERE id = :i", i=room_id) == "after"


# --------------------------------------------------------------------------- #
# Room creation (still in the baseline): its reach rule
# --------------------------------------------------------------------------- #

def test_room_creation_names_only_agents_its_creator_may_reach(rooms):
    """Every agent named must be inside the creator's reach. One agent outside
    it refuses the whole room, an unreachable agent and an unknown one get the
    same refusal, and no room is made. (The creating agent is seated as the
    room's creator, so it names only the others.)"""
    for outside in (C, F, P, UNKNOWN_AGENT):
        response = _create(rooms, rooms.agent, [B, outside])
        assert (response.status_code, response.json()) == (403, _not_accessible(outside)), outside
    assert _room_count() == 0

    for agents in ([B], [D], [B, D]):          # its edge target, its spawn, both
        response = _create(rooms, rooms.agent, agents, name=f"room-{'-'.join(agents)}")
        assert response.status_code == 200, (agents, response.text)
    assert _room_count() == 3

    for person in (rooms.session, rooms.user_key):
        # A person reaches every agent the platform ACL admits; an unknown one
        # is still refused, with the same answer as an unreachable one.
        before = _room_count()
        response = _create(rooms, person, [C, UNKNOWN_AGENT])
        assert (response.status_code, response.json()) == (403, _not_accessible(UNKNOWN_AGENT))
        assert _room_count() == before
        response = _create(rooms, person, [C, F, P])
        assert response.status_code == 200, response.text
    assert _room_count() == 5


@pytest.mark.parametrize("person", ["session", "user_key"])
def test_a_person_still_uses_every_room_route(rooms, person):
    headers = getattr(rooms, person)
    created = _create(rooms, headers, [C], name="by-a-person")
    assert created.status_code == 200, created.text
    room_id = created.json()["id"]
    for key, body in ((S1C[0], None), (S1C[1], None), (S1C[2], {"name": "renamed"})):
        method, url = _path(key, room_id)
        response = rooms.client.request(method, url, json=body, headers=headers)
        assert response.status_code == 200, (key, response.text)


# --------------------------------------------------------------------------- #
# The event loopback reaches none of them
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("key", (CREATE,) + S1C)
def test_the_event_loopback_is_refused_on_every_room_use(rooms, key):
    """A real, signed loopback JWT with no source agent is stopped by the
    room door's own authentication, and nothing is created or renamed."""
    from jose import jwt

    room_id = _create(rooms, rooms.session, [A], name="kept").json()["id"]
    token = jwt.encode(
        {"sub": OWNER, "scope": rooms.deps["EVENT_LOOPBACK_SCOPE"], "exp": int(time.time()) + 300},
        rooms.deps["SECRET_KEY"], algorithm=rooms.deps["ALGORITHM"],
    )
    body = ({"name": "Ops", "agents": [B]} if key == CREATE
            else {"name": "taken"} if key == S1C[2] else None)
    method, url = _path(key, room_id)
    response = rooms.client.request(method, url, json=body, headers=rooms.as_(token))
    assert (response.status_code, response.json()) == (403, {"detail": LOOPBACK_FENCE_DETAIL})
    assert _room_count() == 1
    assert scalar("SELECT name FROM enterprise_rooms WHERE id = :i", i=room_id) == "kept"


# --------------------------------------------------------------------------- #
# Other shape: empty data — an agent in no room
# --------------------------------------------------------------------------- #

def test_an_agent_in_no_room_gets_an_empty_list_and_the_missing_room_answer(rooms):
    """A fresh instance with no rooms at all: the agent's own key lists `[]`
    and reading any id is the uniform 404, never a 500."""
    method, url = _path("shared_sessions/router.py::list_rooms")
    response = rooms.client.request(method, url, headers=rooms.agent)
    assert (response.status_code, response.json()) == (200, {"rooms": []})

    method, url = _path("shared_sessions/router.py::get_room", MISSING_ROOM)
    response = rooms.client.request(method, url, headers=rooms.agent)
    assert (response.status_code, response.json()) == (404, NOT_FOUND)
