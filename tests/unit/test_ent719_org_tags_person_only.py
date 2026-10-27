"""trinity-enterprise#719 — the org-chart tags take a person.

Tags under `dept-*` and `reports-to-*` carry the Grid's org chart, which
trinity-enterprise#305 ruled human-only. The three tag-write routes
(`PUT /api/agents/{name}/tags`, `POST …/tags/{tag}`, `DELETE …/tags/{tag}`)
check the write in `routers/tags.py::_guard_org_namespace`: any change that
touches a reserved prefix is refused unless the caller is a person
(`dependencies.is_person_principal`: a signed-in session or the person's own
`user`-scoped key). It is an allowlist, so the system key and every other
non-person scope are refused like an agent key.

The other writer is a system manifest: its name is applied to every agent it
creates as a tag, so `services/system_service.py::validate_manifest` refuses a
system name under a reserved prefix for every caller, with the same named
message its tag fields already get (a manifest never draws the org chart).

Everything else about tags is unchanged. A plain tag still works on the
agent's own key and on the system key, the PUT still checks only the delta (an
agent may rewrite its own plain tags while its org tags ride along unchanged),
and the routes stay in the census baseline because the guard is conditional on
the tag value, not on the route.

Everything goes through the real tags router, and the manifest cases through
the real systems router. Credentials are real wherever the platform can mint
them (JWTs, the owner's `user` key, agent keys, the system key, connector /
ops / portal-delegate keys, a signed loopback JWT), seeded in
the real schema; the principals the real path fences earlier, or cannot mint,
are stated as real `models.User` values through `get_current_user`. Every
tag-route refusal asserts the stored tags did not move from a non-ambient seed;
every admission asserts they did. A manifest refusal asserts the named 400; on
a deploy, which would otherwise create the agent and write its tag, it also
asserts that nothing was created and no tag was written.

Sync throughout (`tests/unit/pytest.ini` runs pytest-asyncio in strict mode).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_UNIT = Path(__file__).resolve().parent
_BACKEND = _UNIT.parents[1] / "src" / "backend"
for _p in (str(_BACKEND), str(_UNIT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _route_census as rc  # noqa: E402
import dependencies  # noqa: E402
import routers.systems as _SYSTEMS  # noqa: E402
import routers.tags as _TAGS  # noqa: E402
import services.agent_service.crud as _CRUD  # noqa: E402
import services.system_service as _SYSTEM  # noqa: E402
from database import db  # noqa: E402
from db_harness import db_backend  # noqa: E402,F401 — the fixture, used by name
from db_harness import run as _hrun  # noqa: E402
from db_models import McpApiKeyCreate, UserCreate  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from models import User  # noqa: E402

pytestmark = pytest.mark.unit

OWNER = "tg719-owner"  # an admin: the default admin-owned install
OWNER_EMAIL = "tg719-owner@example.com"
AGENT = "tg719-agent"  # the agent whose tags are written
PARENT = "tg719-parent"  # spawned CHILD with its current key (manage reach)
CHILD = "tg719-child"
EDGE = "tg719-edge"  # holds a call edge to AGENT: use reach, not manage reach
SYSTEM_AGENT = "tg719-system"  # the agent the system-scoped key belongs to
FRESH = "tg719-fresh"  # an agent with no tags

SEED = ["dept-sales", "plain", "reports-to-boss"]
LOOPBACK_FENCE_DETAIL = "Event-loopback tokens may only dispatch a subscriber task"

TAG_WRITE_KEYS = {
    "routers/tags.py::set_agent_tags",
    "routers/tags.py::add_agent_tag",
    "routers/tags.py::remove_agent_tag",
}

# Every write that touches a reserved prefix, and the tags it leaves when it lands.
RESERVED = {
    "put_adds_dept": (
        ("PUT", "", {"tags": SEED + ["dept-ops"]}),
        ["dept-ops", "dept-sales", "plain", "reports-to-boss"],
    ),
    "put_drops_both": (("PUT", "", {"tags": ["plain"]}), ["plain"]),
    "post_dept": (("POST", "/dept-ops", None), ["dept-ops", "dept-sales", "plain", "reports-to-boss"]),
    "post_reports_to": (
        ("POST", "/reports-to-ceo", None),
        ["dept-sales", "plain", "reports-to-boss", "reports-to-ceo"],
    ),
    "delete_dept": (("DELETE", "/dept-sales", None), ["plain", "reports-to-boss"]),
    "delete_reports_to": (("DELETE", "/reports-to-boss", None), ["dept-sales", "plain"]),
}
RESERVED_IDS = list(RESERVED)


# --------------------------------------------------------------------------- #
# World
# --------------------------------------------------------------------------- #


@pytest.fixture
def world(db_backend, monkeypatch):  # noqa: F811 — the db_harness fixture
    monkeypatch.setattr(dependencies, "get_breaker_redis", lambda: None)
    monkeypatch.setattr(_TAGS, "manager", None)  # no tag-change broadcast

    db.create_user(UserCreate(username=OWNER, role="admin", email=OWNER_EMAIL))
    for agent in (AGENT, PARENT, EDGE, SYSTEM_AGENT, FRESH):
        db.register_agent_owner(agent, OWNER)
    parent_key = db.create_agent_mcp_api_key(PARENT, OWNER)
    db.register_agent_owner(
        CHILD, OWNER, spawned_by_agent=PARENT, spawned_by_key_id=parent_key.id
    )
    db.add_agent_permission(EDGE, AGENT, OWNER)  # EDGE may call AGENT
    # Non-ambient seeds: an org department, a reporting line and a plain tag.
    for agent in (AGENT, CHILD):
        db.set_agent_tags(agent, SEED)

    app = FastAPI()
    app.include_router(_TAGS.router)
    client = TestClient(app)

    def as_(principal):
        app.dependency_overrides[dependencies.get_current_user] = lambda: principal

    yield SimpleNamespace(client=client, app=app, as_=as_, parent_key=parent_key.api_key)
    app.dependency_overrides.clear()


def _tags(agent: str = AGENT) -> list:
    return sorted(db.get_agent_tags(agent))


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _jwt(username: str = OWNER) -> dict:
    return _auth(dependencies.create_access_token({"sub": username}))


def _agent_key(agent: str = AGENT) -> dict:
    return _auth(db.create_agent_mcp_api_key(agent, OWNER).api_key)


def _system_key() -> dict:
    """Created the way the platform creates it: an agent key under the admin,
    then its scope column set to `system` (system_agent_service._set_system_scope)."""
    key = db.create_agent_mcp_api_key(SYSTEM_AGENT, OWNER)
    _hrun("UPDATE mcp_api_keys SET scope = 'system' WHERE id = :i", i=key.id)
    return _auth(key.api_key)


def _owner_user_key() -> dict:
    return _auth(db.create_mcp_api_key(OWNER, McpApiKeyCreate(name="owner key")).api_key)


def _scoped_user_key(scope: str) -> dict:
    return _auth(
        db.create_mcp_api_key(OWNER, McpApiKeyCreate(name=f"{scope} key", scope=scope)).api_key
    )


def _connector_key() -> dict:
    owner_id = db.get_user_by_username(OWNER)["id"]
    return _auth(db.mint_connector_key(AGENT, owner_id)["api_key"])


def _owner(**fields) -> User:
    base = {"id": db.get_user_by_username(OWNER)["id"], "username": OWNER,
            "email": OWNER_EMAIL, "role": "admin"}
    base.update(fields)
    return User(**base)


def _send(world, method: str, suffix: str, body, headers=None, agent: str = AGENT):
    url = f"/api/agents/{agent}/tags{suffix}"
    return world.client.request(method, url, json=body, headers=headers or {})


def _write(world, route: str, headers=None, agent: str = AGENT):
    (method, suffix, body), _ = RESERVED[route]
    return _send(world, method, suffix, body, headers, agent)


def _assert_guard_refused(res) -> None:
    assert res.status_code == 403, res.text
    detail = res.json()["detail"]
    assert isinstance(detail, str) and "org chart" in detail and "Blocked:" in detail, detail


# --------------------------------------------------------------------------- #
# 1. The census: the guard is value-conditional, so the routes stay listed
# --------------------------------------------------------------------------- #


def test_the_tag_write_routes_stay_in_the_baseline():
    baseline = rc.load_baseline()
    for key in sorted(TAG_WRITE_KEYS):
        assert key in baseline, key


# --------------------------------------------------------------------------- #
# 2. Non-persons are refused on every reserved-prefix write
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("route", RESERVED_IDS)
@pytest.mark.parametrize(
    "headers",
    [
        pytest.param(lambda world: _system_key(), id="system-key"),
        pytest.param(lambda world: _agent_key(AGENT), id="agent-own-key"),
    ],
)
def test_a_machine_key_is_refused_by_the_guard(world, route, headers):
    res = _write(world, route, headers(world))
    _assert_guard_refused(res)
    assert _tags() == sorted(SEED)


@pytest.mark.parametrize("route", RESERVED_IDS)
def test_an_agent_key_on_an_agent_it_spawned_is_refused_by_the_guard(world, route):
    """The parent manages the child it spawned (ent#629's manage reach), so the
    owner dependency admits it and the guard is what refuses it."""
    res = _write(world, route, _auth(world.parent_key), agent=CHILD)
    _assert_guard_refused(res)
    assert _tags(CHILD) == sorted(SEED)


@pytest.mark.parametrize("route", RESERVED_IDS)
def test_an_edge_sibling_is_refused_by_the_owner_dependency(world, route):
    """Unchanged: a call edge is "may call", never "may manage" (ent#629)."""
    res = _write(world, route, _agent_key(EDGE))
    assert res.status_code == 404, res.text
    assert _tags() == sorted(SEED)


@pytest.mark.parametrize("route", RESERVED_IDS)
@pytest.mark.parametrize(
    "headers",
    [
        pytest.param(lambda: _connector_key(), id="connector-key"),
        pytest.param(lambda: _scoped_user_key("ops"), id="ops-key"),
        pytest.param(lambda: _scoped_user_key("portal_delegate"), id="portal-delegate-key"),
    ],
)
def test_a_real_fenced_key_is_refused_before_the_route(world, route, headers):
    res = _write(world, route, headers())
    assert res.status_code == 403, res.text
    assert _tags() == sorted(SEED)


@pytest.mark.parametrize("route", RESERVED_IDS)
def test_an_unvouched_loopback_jwt_is_refused_by_the_entry_fence(world, route, monkeypatch):
    """A real, signed loopback JWT with NO source agent: the entry fence refuses
    it on every route but the subscriber-task dispatch (ent#614). The subject is
    a live admin and the token is not revoked, so a pass would be a 2xx."""
    from jose import jwt

    from config import ALGORITHM, SECRET_KEY

    monkeypatch.setattr(dependencies, "is_token_revoked", lambda *_a, **_k: False)
    token = jwt.encode(
        {"sub": OWNER, "scope": dependencies.EVENT_LOOPBACK_SCOPE, "exp": int(time.time()) + 300},
        SECRET_KEY,
        algorithm=ALGORITHM,
    )
    res = _write(world, route, _auth(token))
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == LOOPBACK_FENCE_DETAIL
    assert _tags() == sorted(SEED)


# Principals the guard must refuse that the real path fences earlier or cannot
# mint as a key row: stated, never a MagicMock.
STATED_NON_PERSONS = [
    pytest.param({"mcp_scope": "system"}, id="system-key"),
    pytest.param({"mcp_scope": "ops"}, id="ops-key"),
    pytest.param({"mcp_scope": "portal_delegate", "portal_delegate": True}, id="portal-delegate-key"),
    pytest.param({"mcp_scope": "a-scope-shipped-tomorrow"}, id="unknown-future-scope"),
    pytest.param({"mcp_scope": "user", "agent_name": AGENT}, id="user-scope-carrying-an-agent"),
    pytest.param(
        {"mcp_scope": None, "is_event_loopback": True, "vouched_source_agent": AGENT},
        id="vouched-loopback-jwt",
    ),
    pytest.param({"mcp_scope": None, "is_event_loopback": True}, id="unvouched-loopback-jwt"),
]


@pytest.mark.parametrize("route", RESERVED_IDS)
@pytest.mark.parametrize("fields", STATED_NON_PERSONS)
def test_every_non_person_is_refused_by_the_guard(world, route, fields):
    world.as_(_owner(**fields))
    res = _write(world, route)
    _assert_guard_refused(res)
    assert _tags() == sorted(SEED)


@pytest.mark.parametrize("route", RESERVED_IDS)
def test_a_connector_principal_is_refused(world, route):
    """Refused by the owner dependency's connector fence before the guard runs."""
    world.as_(_owner(mcp_scope="connector", connector_agent=AGENT))
    res = _write(world, route)
    assert res.status_code == 403, res.text
    assert _tags() == sorted(SEED)


class _NoScopeAttribute:
    """An admin principal with NO `mcp_scope` attribute at all: the allowlist must
    fail closed (sentinel, never `getattr(..., None)`, the #2323 trap)."""

    username = OWNER
    email = OWNER_EMAIL
    role = "admin"
    agent_name = None
    connector_agent = None
    portal_delegate = False

    def __init__(self, user_id: int):
        self.id = user_id


@pytest.mark.parametrize("route", RESERVED_IDS)
def test_a_principal_without_mcp_scope_fails_closed(world, route):
    world.as_(_NoScopeAttribute(db.get_user_by_username(OWNER)["id"]))
    res = _write(world, route)
    _assert_guard_refused(res)
    assert _tags() == sorted(SEED)


# --------------------------------------------------------------------------- #
# 3. A person still draws the org chart
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("route", RESERVED_IDS)
@pytest.mark.parametrize(
    "headers",
    [
        pytest.param(lambda: _jwt(OWNER), id="owner-session"),
        pytest.param(lambda: _owner_user_key(), id="owner-user-key"),
    ],
)
def test_a_person_passes(world, route, headers):
    res = _write(world, route, headers())
    assert res.status_code == 200, res.text
    expected = RESERVED[route][1]
    assert sorted(res.json()["tags"]) == expected
    assert _tags() == expected


# --------------------------------------------------------------------------- #
# 4. Over-correction guard: plain tags still work for machine keys
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param(lambda: _agent_key(AGENT), id="agent-own-key"),
        pytest.param(lambda: _system_key(), id="system-key"),
    ],
)
def test_a_plain_tag_still_works_on_a_machine_key(world, headers):
    h = headers()
    res = _send(world, "POST", "/plain-two", None, h)
    assert res.status_code == 200, res.text
    assert _tags() == sorted(SEED + ["plain-two"])

    res = _send(world, "DELETE", "/plain", None, h)
    assert res.status_code == 200, res.text
    assert _tags() == ["dept-sales", "plain-two", "reports-to-boss"]

    # The PUT checks the delta: the org tags ride along unchanged.
    res = _send(world, "PUT", "", {"tags": ["dept-sales", "reports-to-boss", "renamed"]}, h)
    assert res.status_code == 200, res.text
    assert _tags() == ["dept-sales", "renamed", "reports-to-boss"]


# --------------------------------------------------------------------------- #
# 5. An agent with no tags
# --------------------------------------------------------------------------- #


def test_an_agent_with_no_tags(world):
    owner = _jwt(OWNER)
    res = world.client.get(f"/api/agents/{FRESH}/tags", headers=owner)
    assert res.status_code == 200 and res.json()["tags"] == [], res.text

    res = _send(world, "POST", "/dept-ops", None, _system_key(), agent=FRESH)
    _assert_guard_refused(res)
    res = _send(world, "DELETE", "/reports-to-boss", None, _agent_key(FRESH), agent=FRESH)
    _assert_guard_refused(res)
    assert _tags(FRESH) == []

    res = _send(world, "POST", "/plain", None, _agent_key(FRESH), agent=FRESH)
    assert res.status_code == 200, res.text
    assert _tags(FRESH) == ["plain"]

    res = _send(world, "POST", "/dept-ops", None, owner, agent=FRESH)
    assert res.status_code == 200, res.text
    assert _tags(FRESH) == ["dept-ops", "plain"]


# --------------------------------------------------------------------------- #
# 6. The other writer: a system manifest's name is applied as a tag
# --------------------------------------------------------------------------- #

RESERVED_SYSTEM_NAMES = ["dept-sales", "reports-to-ceo"]


def _agent_key_holding_agents_manage() -> dict:
    """An agent key the `agents.manage` capability fence on the deploy route admits."""
    db.grant_agent_capability(AGENT, "agents.manage", OWNER)
    return _agent_key(AGENT)


DEPLOYERS = [
    pytest.param(lambda: _jwt(OWNER), id="owner-session"),
    pytest.param(lambda: _owner_user_key(), id="owner-user-key"),
    pytest.param(lambda: _system_key(), id="system-key"),
    pytest.param(lambda: _agent_key_holding_agents_manage(), id="agent-key-holding-agents-manage"),
]


def _manifest(system_name: str) -> str:
    return f"name: {system_name}\nagents:\n  worker:\n    template: local:default\n"


@pytest.fixture
def deploy(world, monkeypatch):
    """The real systems router behind the real door. Agent creation and the
    container-facing steps are stubbed (a created agent is registered, as the
    real create does); `configure_tags` is real and writes to the real schema."""
    created: list = []

    async def _create(config, current_user, request, **_kwargs):
        db.register_agent_owner(config.name, OWNER)
        created.append(config.name)
        return {"status": "created"}

    async def _start(names):
        return {name: "started" for name in names}

    monkeypatch.setattr(_SYSTEM, "_default_create_agent_fn", lambda: _create)
    monkeypatch.setattr(_SYSTEM, "configure_folders", lambda **_kwargs: 0)
    monkeypatch.setattr(_SYSTEM, "create_schedules", lambda **_kwargs: 0)
    monkeypatch.setattr(_SYSTEM, "start_all_agents", _start)
    # `local:` templates resolve; the catalog root does not exist under pytest.
    monkeypatch.setattr(_CRUD, "_resolve_local_template", lambda config: ({}, None))
    world.app.include_router(_SYSTEMS.router)
    world.created = created
    return world


def _deploy(world, system_name: str, headers: dict, *, dry_run: bool):
    body = {"manifest": _manifest(system_name), "dry_run": dry_run}
    return world.client.post("/api/systems/deploy", json=body, headers=headers)


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "deploy"])
@pytest.mark.parametrize("system_name", RESERVED_SYSTEM_NAMES)
@pytest.mark.parametrize("headers", DEPLOYERS)
def test_a_system_name_under_a_reserved_prefix_is_refused_for_everyone(
    deploy, headers, system_name, dry_run
):
    """A person and the system key alike: a manifest never draws the org chart,
    the same rule its `default_tags` and per-agent tags already follow."""
    holders = db.get_agents_by_tag(system_name)  # the seed carries `dept-sales`
    res = _deploy(deploy, system_name, headers(), dry_run=dry_run)
    assert res.status_code == 400, res.text
    detail = res.json()["detail"]
    assert detail.startswith(
        f"System name uses reserved org-overlay tag '{system_name}'"
    ), detail
    assert "carry the org chart" in detail, detail
    assert deploy.created == []
    assert _tags(f"{system_name}-worker") == []
    assert db.get_agents_by_tag(system_name) == holders


@pytest.mark.parametrize("headers", DEPLOYERS)
def test_a_plain_system_name_still_deploys_and_tags_its_agents(deploy, headers):
    """Over-correction guard, and proof that each deployer above reached the
    validator: a plain system name deploys and becomes its agents' tag."""
    res = _deploy(deploy, "sales-desk", headers(), dry_run=False)
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "deployed", res.json()
    assert deploy.created == ["sales-desk-worker"]
    assert _tags("sales-desk-worker") == ["sales-desk"]
