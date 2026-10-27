"""trinity-enterprise#719 — a schedule's webhook takes a person or the grant.

A schedule's webhook URL (and its signing secret) is a credential an outside
caller presents to start the schedule, and a webhook run is not gated by the
agent's autonomy (`feature-flows/autonomy-mode.md`, "Scope"). The own-agent
exemption on `schedules.manage` (ent#164) rests on "its own use, bounded by
autonomy", so it covers the schedule but not its webhook. The five webhook
routes therefore take `Depends(require_person_or_capability("schedules.manage"))`
in the decorator:

* a person — a signed-in session or the person's own `user`-scoped key — passes;
* an agent key holding `schedules.manage` passes, on itself or on an agent its
  owner owns (the #3236 reach bound: any other target answers 404);
* an agent key without the grant gets the named 403
  `schedule_management_not_permitted` and one `capability_refused` audit row,
  on its own agent too, before the agent or schedule is looked up;
* the system key gets 403 `person_required`. Connector, ops, portal-delegate
  and ephemeral (ghost) agent keys are refused first by their own entry
  fences in `get_current_user`; the route gate refuses every other non-person
  that reaches it (one that is not an agent key) with `person_required`.

The read (`GET …/webhook`) returns the URL with its token, so it carries the
same gate as the writes. The schedule's own create / update / enable / disable /
delete keep the own-agent exemption, and are pinned here as unchanged.

Two harnesses, as in `test_2996_owner_config_person_only.py`: the REAL
`get_current_user` with key rows seeded in the real schema (agent keys, the
system key, the owner's user key, JWTs, a real loopback JWT), and an override
with a stated `models.User` for the principals the real path fences earlier or
cannot mint. Every refusal on a seeded webhook asserts the stored state did not
move, from a non-ambient seed; every admission asserts it did. Everything goes through the
real schedules router. Sync throughout (`tests/unit/pytest.ini` runs
pytest-asyncio in strict mode).

Related: docs/memory/requirements/auth.md §2.9, feature-flows/self-change-grants.md.
"""

from __future__ import annotations

import os
import secrets
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

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from db_harness import db_backend  # noqa: E402,F401 — the fixture, used by name
from db_harness import count as _count  # noqa: E402
from db_harness import run as _hrun  # noqa: E402
from db_harness import scalar as _scalar  # noqa: E402

import _route_census as rc  # noqa: E402
import dependencies  # noqa: E402
import routers.schedules as _SCHED  # noqa: E402
from database import db  # noqa: E402
from db_models import McpApiKeyCreate, ScheduleCreate, UserCreate  # noqa: E402
from models import User  # noqa: E402

pytestmark = pytest.mark.unit

OWNER = "wh719-owner"  # an admin: the default admin-owned install
OTHER = "wh719-other"  # another user
AGENT = "wh719-agent"  # the agent whose schedule carries the webhook
ORCH = "wh719-orch"  # a sibling its owner owns (the grant holder)
EDGE = "wh719-edge"  # a sibling holding a call edge to AGENT (use reach)
FOREIGN = "wh719-foreign"  # an agent another user owns
SYSTEM = "wh719-system"  # the agent the system-scoped key belongs to
GHOST = "wh719-no-such-agent"

CAP = "schedules.manage"
REFUSAL_CODE = "schedule_management_not_permitted"
LOOPBACK_FENCE_DETAIL = "Event-loopback tokens may only dispatch a subscriber task"

# The five webhook routes: (method, path suffix after the schedule id).
ROUTES = {
    "mint": ("POST", "/webhook"),
    "read": ("GET", "/webhook"),
    "sign": ("POST", "/webhook/secret"),
    "unsign": ("DELETE", "/webhook/secret"),
    "revoke": ("DELETE", "/webhook"),
}
ROUTE_IDS = list(ROUTES)

WEBHOOK_KEYS = {
    "routers/schedules.py::generate_webhook",
    "routers/schedules.py::get_webhook_status",
    "routers/schedules.py::generate_webhook_secret",
    "routers/schedules.py::disable_webhook_secret",
    "routers/schedules.py::revoke_webhook",
}


# --------------------------------------------------------------------------- #
# World
# --------------------------------------------------------------------------- #


def _new_schedule(agent: str, owner: str) -> str:
    schedule = db.create_schedule(
        agent,
        owner,
        ScheduleCreate(
            name=f"nightly-{secrets.token_hex(3)}",
            cron_expression="0 3 * * *",
            message="run",
            enabled=False,
        ),
    )
    assert schedule is not None
    return schedule.id


@pytest.fixture
def world(db_backend, monkeypatch):  # noqa: F811 — the db_harness fixture
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))
    monkeypatch.setattr(dependencies, "get_breaker_redis", lambda: None)
    db.create_user(
        UserCreate(username=OWNER, role="admin", email="wh719-owner@example.com")
    )
    db.create_user(
        UserCreate(username=OTHER, role="operator", email="wh719-other@example.com")
    )
    for agent in (AGENT, ORCH, EDGE):
        db.register_agent_owner(agent, OWNER)
    db.register_agent_owner(FOREIGN, OTHER)
    db.add_agent_permission(EDGE, AGENT, OWNER)  # EDGE may call AGENT
    owner_id = db.get_user_by_username(OWNER)["id"]

    app = FastAPI()
    app.include_router(_SCHED.router)
    client = TestClient(app)

    def as_(**fields):
        base = {
            "id": owner_id,
            "username": OWNER,
            "email": "wh719-owner@example.com",
            "role": "admin",
        }
        base.update(fields)
        app.dependency_overrides[dependencies.get_current_user] = lambda: User(**base)

    yield SimpleNamespace(
        client=client,
        app=app,
        as_=as_,
        schedule=_new_schedule(AGENT, OWNER),
        foreign_schedule=_new_schedule(FOREIGN, OTHER),
    )
    app.dependency_overrides.clear()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _jwt(username: str = OWNER) -> dict:
    return _auth(dependencies.create_access_token({"sub": username}))


def _agent_key(agent: str = AGENT) -> dict:
    return _auth(db.create_agent_mcp_api_key(agent, OWNER).api_key)


def _system_key() -> dict:
    """Created the way the platform creates it: an agent key under the admin,
    then its scope column set to `system` (system_agent_service._set_system_scope)."""
    key = db.create_agent_mcp_api_key(SYSTEM, OWNER)
    _hrun("UPDATE mcp_api_keys SET scope = 'system' WHERE id = :i", i=key.id)
    return _auth(key.api_key)


def _owner_user_key() -> dict:
    return _auth(
        db.create_mcp_api_key(OWNER, McpApiKeyCreate(name="owner key")).api_key
    )


def _grant(agent: str) -> None:
    db.grant_agent_capability(agent, CAP, OWNER)


# --------------------------------------------------------------------------- #
# Seeds and the stored state each route moves
# --------------------------------------------------------------------------- #


def _seed(route: str, schedule_id: str) -> dict:
    """A non-ambient start: every schedule has a live token; `unsign` also has a
    secret, so each route has something to move."""
    db.generate_webhook_token(schedule_id)
    if route == "unsign":
        assert db.set_webhook_secret(schedule_id)
    return _state(schedule_id)


def _state(schedule_id: str) -> dict:
    status = db.get_webhook_status(schedule_id)
    return {
        "token": status["webhook_token"],
        "has_secret": status["has_secret"],
        "auth_enabled": status["auth_enabled"],
    }


def _moved(route: str, before: dict, after: dict) -> bool:
    if route == "mint":
        return after["token"] not in (None, before["token"])
    if route == "read":
        return after == before
    if route == "sign":
        return after["has_secret"] and after["auth_enabled"]
    if route == "unsign":
        return not after["has_secret"] and not after["auth_enabled"]
    if route == "revoke":
        return after["token"] is None
    raise AssertionError(route)


def _call(world, route: str, agent: str, schedule_id: str, headers=None):
    method, suffix = ROUTES[route]
    url = f"/api/agents/{agent}/schedules/{schedule_id}{suffix}"
    return world.client.request(method, url, headers=headers or {})


def _refusals() -> int:
    return _count("audit_log", "event_action = 'capability_refused'")


def _is_capability_refusal(res) -> bool:
    detail = res.json().get("detail")
    return (
        res.status_code == 403
        and isinstance(detail, dict)
        and detail.get("code") == REFUSAL_CODE
        and detail.get("capability") == CAP
    )


def _is_person_required(res) -> bool:
    detail = res.json().get("detail")
    return (
        res.status_code == 403
        and isinstance(detail, dict)
        and detail.get("code") == "person_required"
    )


def _no_token_in(res, token: str) -> None:
    assert token not in res.text, "a refused caller was shown the webhook token"


def _assert_admitted(world, res, route: str, before: dict) -> None:
    assert 200 <= res.status_code < 300, res.text
    after = _state(world.schedule)
    assert _moved(route, before, after), (route, before, after)
    if route == "read":
        body = res.json()
        assert body["has_token"] is True
        assert body["webhook_url"].endswith(f"/api/webhooks/{before['token']}")


# --------------------------------------------------------------------------- #
# 1. The agent's own key without the grant: the named refusal, audited once
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("route", ROUTE_IDS)
def test_an_ungranted_agent_key_is_refused_on_its_own_agent_and_audited_once(
    world, route
):
    before = _seed(route, world.schedule)
    assert _refusals() == 0
    res = _call(world, route, AGENT, world.schedule, _agent_key())
    assert _is_capability_refusal(res), res.text
    _no_token_in(res, before["token"])
    assert _state(world.schedule) == before
    assert _refusals() == 1
    row = _scalar(
        "SELECT actor_type || '|' || actor_id || '|' || target_id || '|' || endpoint "
        "FROM audit_log WHERE event_action = 'capability_refused'"
    )
    method, suffix = ROUTES[route]
    assert (
        row
        == f"agent|{AGENT}|{AGENT}|/api/agents/{AGENT}/schedules/{world.schedule}{suffix}"
    )


@pytest.mark.parametrize("route", ROUTE_IDS)
def test_an_ungranted_sibling_inside_its_reach_is_refused(world, route):
    """A call edge does not authorise webhook management: EDGE holds an edge to
    AGENT and is refused on every webhook route, the read included."""
    before = _seed(route, world.schedule)
    res = _call(world, route, AGENT, world.schedule, _agent_key(EDGE))
    assert _is_capability_refusal(res), res.text
    _no_token_in(res, before["token"])
    assert _state(world.schedule) == before
    assert _refusals() == 1


@pytest.mark.parametrize("route", ROUTE_IDS)
def test_the_refusal_comes_before_any_lookup(world, route):
    """#186: an ungranted key gets the same 403 for an agent that does not exist
    and for a schedule that does not exist — never a 404 that tells them apart."""
    for agent, schedule_id in ((GHOST, world.schedule), (AGENT, "no-such-schedule")):
        res = _call(world, route, agent, schedule_id, _agent_key())
        assert _is_capability_refusal(res), (agent, res.text)


# --------------------------------------------------------------------------- #
# 2. The grant: the agent itself, or a sibling its owner owns
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("route", ROUTE_IDS)
def test_a_granted_agent_key_manages_its_own_webhook(world, route):
    before = _seed(route, world.schedule)
    _grant(AGENT)
    res = _call(world, route, AGENT, world.schedule, _agent_key())
    _assert_admitted(world, res, route, before)
    assert _refusals() == 0


@pytest.mark.parametrize("route", ROUTE_IDS)
def test_a_granted_sibling_manages_a_webhook_on_an_agent_its_owner_owns(world, route):
    before = _seed(route, world.schedule)
    _grant(ORCH)
    res = _call(world, route, AGENT, world.schedule, _agent_key(ORCH))
    _assert_admitted(world, res, route, before)


@pytest.mark.parametrize("route", ROUTE_IDS)
def test_a_holder_reaches_only_agents_its_owner_owns(world, route):
    """#3236's bound holds on the webhook routes: another user's agent and an
    agent that does not exist answer the same 404, and nothing moves."""
    before = _seed(route, world.foreign_schedule)
    _grant(ORCH)
    for agent, schedule_id in (
        (FOREIGN, world.foreign_schedule),
        (GHOST, world.foreign_schedule),
    ):
        res = _call(world, route, agent, schedule_id, _agent_key(ORCH))
        assert res.status_code == 404, (agent, res.text)
        _no_token_in(res, before["token"])
    assert _state(world.foreign_schedule) == before


@pytest.mark.parametrize("route", ROUTE_IDS)
def test_a_call_edge_does_not_widen_a_holders_reach(world, route):
    """A call edge (`agent_permissions`) widens what an agent may use, not what
    a holder may manage: ORCH holds the grant and an edge to another user's
    agent, and still gets the same 404 as any agent outside its owner's, the
    read included. Nothing is shown and nothing moves."""
    before = _seed(route, world.foreign_schedule)
    _grant(ORCH)
    db.add_agent_permission(ORCH, FOREIGN, OWNER)  # ORCH may call FOREIGN
    # The premise, so the 404 below cannot come from a missing edge: the edge is
    # stored, and ORCH's principal passes the read's own path dependency
    # (`AuthorizedAgent`) for FOREIGN through it.
    assert db.is_agent_permitted(ORCH, FOREIGN)
    orch = User(id=db.get_user_by_username(OWNER)["id"], username=OWNER, role="admin",
                agent_name=ORCH, mcp_scope="agent")
    assert dependencies.get_authorized_agent(name=FOREIGN, current_user=orch) == FOREIGN
    res = _call(world, route, FOREIGN, world.foreign_schedule, _agent_key(ORCH))
    assert res.status_code == 404, res.text
    _no_token_in(res, before["token"])
    assert _state(world.foreign_schedule) == before


# --------------------------------------------------------------------------- #
# 3. A person passes; the system key and every other non-person do not
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("route", ROUTE_IDS)
@pytest.mark.parametrize(
    "headers",
    [
        pytest.param(lambda: _jwt(), id="owner-jwt"),
        pytest.param(lambda: _owner_user_key(), id="owner-user-key"),
    ],
)
def test_a_person_manages_the_webhook(world, route, headers):
    before = _seed(route, world.schedule)
    res = _call(world, route, AGENT, world.schedule, headers())
    _assert_admitted(world, res, route, before)


@pytest.mark.parametrize("route", ROUTE_IDS)
def test_the_system_key_is_refused_as_not_a_person(world, route):
    before = _seed(route, world.schedule)
    for agent in (AGENT, GHOST):
        res = _call(world, route, agent, world.schedule, _system_key())
        assert _is_person_required(res), (agent, res.text)
        _no_token_in(res, before["token"])
    assert _state(world.schedule) == before


GHOST_AGENT = "wh719-ghost"   # an ephemeral (ghost) agent

# Each entry fence's own refusal, asserted exactly. A key that got past its
# fence would meet the route gate instead and get a different answer
# (`person_required`, or the capability refusal for a ghost), so these cases pin
# the entry fence itself, not only the 403.
CONNECTOR_FENCE_DETAIL = (
    "Connector keys may only chat their bound agent and list its playbooks"
)
OPS_FENCE_DETAIL = (
    "Ops keys are read-only and limited to fleet health, telemetry, execution "
    "and subscription reads"
)
PORTAL_DELEGATE_FENCE_DETAIL = (
    "Portal delegate keys may only exchange an end-user email for a portal session"
)
GHOST_FENCE_DETAIL = (
    "Ephemeral agent keys are restricted to heartbeat, result delivery, reports, "
    "notifications, and self-info"
)


def _connector_key() -> dict:
    owner_id = db.get_user_by_username(OWNER)["id"]
    return _auth(db.mint_connector_key(AGENT, owner_id)["api_key"])


def _scoped_user_key(scope: str) -> dict:
    return _auth(db.create_mcp_api_key(OWNER, McpApiKeyCreate(name=f"{scope} key", scope=scope)).api_key)


def _ghost_key() -> dict:
    db.register_agent_owner(GHOST_AGENT, OWNER, is_ephemeral=True, ephemeral_max_executions=5,
                            ephemeral_expires_at="2099-01-01T00:00:00Z")
    return _auth(db.create_agent_mcp_api_key(GHOST_AGENT, OWNER).api_key)


@pytest.mark.parametrize("route", ROUTE_IDS)
@pytest.mark.parametrize("headers,detail", [
    pytest.param(lambda: _connector_key(), CONNECTOR_FENCE_DETAIL, id="connector-key"),
    pytest.param(lambda: _scoped_user_key("ops"), OPS_FENCE_DETAIL, id="ops-key"),
    pytest.param(lambda: _scoped_user_key("portal_delegate"), PORTAL_DELEGATE_FENCE_DETAIL,
                 id="portal-delegate-key"),
    pytest.param(lambda: _ghost_key(), GHOST_FENCE_DETAIL, id="ephemeral-agent-key"),
])
def test_a_real_fenced_key_is_refused_before_the_route(world, route, headers, detail):
    """Through the REAL `get_current_user`: these keys never reach the route
    gate, because their own entry fence answers first, with its own detail and
    no `capability_refused` row. The detail is asserted exactly, so a key that
    got past its fence fails here even though the route gate would still refuse
    it. Nothing moves and the token is not shown."""
    before = _seed(route, world.schedule)
    res = _call(world, route, AGENT, world.schedule, headers())
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == detail
    _no_token_in(res, before["token"])
    assert _state(world.schedule) == before
    assert _refusals() == 0


@pytest.mark.parametrize("route", ROUTE_IDS)
def test_a_ghost_key_is_refused_on_its_own_schedule_too(world, route):
    """The ephemeral entry fence answers on the ghost's own schedule as well.
    Without it, the route gate would refuse the ghost with the capability
    refusal and an audit row instead (a ghost cannot hold a grant)."""
    headers = _ghost_key()
    schedule_id = _new_schedule(GHOST_AGENT, OWNER)
    before = _seed(route, schedule_id)
    res = _call(world, route, GHOST_AGENT, schedule_id, headers)
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == GHOST_FENCE_DETAIL
    _no_token_in(res, before["token"])
    assert _state(schedule_id) == before
    assert _refusals() == 0


# Principals the route gate must refuse that the real path fences earlier or
# cannot mint as a key row: stated, never a MagicMock.
OVERRIDE_REFUSED = [
    pytest.param({"mcp_scope": "ops"}, id="ops-key"),
    pytest.param(
        {"mcp_scope": "portal_delegate", "portal_delegate": True},
        id="portal-delegate-key",
    ),
    pytest.param(
        {"mcp_scope": "connector", "connector_agent": AGENT}, id="connector-key"
    ),
    pytest.param({"mcp_scope": "a-scope-shipped-tomorrow"}, id="unknown-future-scope"),
    pytest.param(
        {"mcp_scope": "user", "agent_name": AGENT}, id="user-scope-carrying-an-agent"
    ),
    pytest.param(
        {"mcp_scope": None, "is_event_loopback": True, "vouched_source_agent": AGENT},
        id="vouched-loopback-jwt",
    ),
    pytest.param(
        {"mcp_scope": "agent", "agent_name": AGENT, "vouched_source_agent": EDGE},
        id="agent-scope-carrying-a-vouched-source",
    ),
]


@pytest.mark.parametrize("route", ROUTE_IDS)
@pytest.mark.parametrize("principal", OVERRIDE_REFUSED)
def test_every_other_principal_is_refused_by_the_route_gate(world, route, principal):
    before = _seed(route, world.schedule)
    world.as_(**principal)
    res = _call(world, route, AGENT, world.schedule)
    assert _is_person_required(res), res.text
    _no_token_in(res, before["token"])
    assert _state(world.schedule) == before


class _NoScopeAttribute:
    """An admin principal with NO `mcp_scope` attribute at all: the allowlist must
    fail closed (sentinel, never `getattr(..., None)`, the #2323 trap)."""

    id = 1
    username = OWNER
    email = "wh719-owner@example.com"
    role = "admin"
    agent_name = None
    connector_agent = None
    portal_delegate = False


@pytest.mark.parametrize("route", ROUTE_IDS)
def test_a_principal_without_mcp_scope_fails_closed(world, route):
    before = _seed(route, world.schedule)
    world.app.dependency_overrides[dependencies.get_current_user] = (
        lambda: _NoScopeAttribute()
    )
    res = _call(world, route, AGENT, world.schedule)
    assert res.status_code == 403, res.text
    assert _state(world.schedule) == before


@pytest.mark.parametrize("route", ROUTE_IDS)
def test_an_unvouched_loopback_jwt_is_refused_by_the_entry_fence(
    world, route, monkeypatch
):
    """A real, signed loopback JWT with NO source agent, through real
    authentication: the `get_current_user` entry fence refuses it on every route
    but the subscriber-task dispatch (ent#614). A pass would be a 2xx, not an
    incidental 401: the subject is a live admin and the token is not revoked."""
    from config import ALGORITHM, SECRET_KEY
    from jose import jwt

    before = _seed(route, world.schedule)
    monkeypatch.setattr(dependencies, "is_token_revoked", lambda *_a, **_k: False)
    token = jwt.encode(
        {
            "sub": OWNER,
            "scope": dependencies.EVENT_LOOPBACK_SCOPE,
            "exp": int(time.time()) + 300,
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )
    res = _call(world, route, AGENT, world.schedule, _auth(token))
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == LOOPBACK_FENCE_DETAIL
    assert _state(world.schedule) == before


# --------------------------------------------------------------------------- #
# 4. Over-correction guard: the schedule itself keeps its own-agent exemption
# --------------------------------------------------------------------------- #


def test_the_agent_key_still_manages_its_own_schedule_without_a_grant(world):
    headers = _agent_key()
    base = f"/api/agents/{AGENT}/schedules"
    assert not db.agent_has_capability(AGENT, CAP)

    res = world.client.post(
        base,
        json={
            "name": "own",
            "cron_expression": "0 4 * * *",
            "message": "run",
            "enabled": False,
        },
        headers=headers,
    )
    assert res.status_code == 201, res.text
    sid = res.json()["id"]
    assert db.get_schedule(sid).agent_name == AGENT

    res = world.client.put(
        f"{base}/{sid}", json={"message": "run later"}, headers=headers
    )
    assert res.status_code == 200, res.text
    assert db.get_schedule(sid).message == "run later"

    res = world.client.post(f"{base}/{sid}/enable", headers=headers)
    assert res.status_code == 200, res.text
    assert db.get_schedule(sid).enabled is True

    res = world.client.post(f"{base}/{sid}/disable", headers=headers)
    assert res.status_code == 200, res.text
    assert db.get_schedule(sid).enabled is False

    res = world.client.delete(f"{base}/{sid}", headers=headers)
    assert res.status_code == 204, res.text
    assert db.get_schedule(sid) is None
    assert _refusals() == 0


# --------------------------------------------------------------------------- #
# 5. Other shape: a schedule that has no webhook yet
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param(lambda: (_grant(AGENT), _agent_key())[1], id="granted-own-key"),
        pytest.param(lambda: (_grant(ORCH), _agent_key(ORCH))[1], id="granted-sibling"),
        pytest.param(lambda: _jwt(), id="owner-jwt"),
    ],
)
def test_a_schedule_with_no_webhook_reads_as_none(world, headers):
    assert db.get_webhook_status(world.schedule)["has_token"] is False
    res = _call(world, "read", AGENT, world.schedule, headers())
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["has_token"] is False
    assert body["webhook_url"] is None
    assert body["has_secret"] is False


def test_an_ungranted_key_cannot_read_even_an_empty_webhook(world):
    res = _call(world, "read", AGENT, world.schedule, _agent_key())
    assert _is_capability_refusal(res), res.text


# --------------------------------------------------------------------------- #
# 6. The shape: the gate sits in the decorator and the census recognises it
# --------------------------------------------------------------------------- #

_CENSUS = rc.walk()


@pytest.mark.parametrize("key", sorted(WEBHOOK_KEYS))
def test_each_webhook_route_is_person_or_grant_in_the_census(key):
    route = _CENSUS.routes[key]
    assert route.code_class == "person_or_grant", (key, route.gates)
    assert key not in rc.load_baseline()


def test_the_gate_runs_before_the_path_dependency():
    """FastAPI runs a decorator dependency before the signature's: the grant is
    decided before `AuthorizedAgent` looks the agent up (#186)."""
    by_endpoint = {
        r.endpoint.__name__: r
        for r in _SCHED.router.routes
        if getattr(r, "endpoint", None) is not None
    }
    for key in WEBHOOK_KEYS:
        route = by_endpoint[key.split("::")[1]]
        first = route.dependant.dependencies[0].call
        assert first.__name__ == "require_person_or_schedules_manage", (key, first)
        names = {d.call.__name__ for d in route.dependant.dependencies}
        assert "capability_fence_schedules_manage" not in names, key
