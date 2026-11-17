"""trinity-enterprise#719 — channel bindings and their exposure are human-only.

The rule each route here now follows (#2996, Invariant #8, `architecture/security.md` §6):

* Binding a channel credential takes a signed-in session:
  `Depends(require_interactive)`.
* Every other channel write, the setup checks, and the reads that return a webhook
  secret or name a person take a person: `Depends(require_person)`. A person is a
  signed-in session or the person's own `user`-scoped key.

Both gates sit in the route decorator. FastAPI runs them before the path's
ownership lookup, so the refusal depends only on the principal: a nonexistent
agent gets the same 403 as a real one (#186).

Every case goes through the real routers (`routers/{telegram,whatsapp,voip,slack}.py`
mounted on one app):

* REAL `get_current_user`, with real key rows in the real schema
  (`tests/db_harness.py`) and real JWTs. The keys are the agent's own key, a
  sibling agent's key on an agent it spawned (inside its manage reach,
  trinity-enterprise#629), the system key, and the owner's `user` key.
* An override of `get_current_user` with a stated principal, for the scopes the
  real path fences earlier or cannot mint: ops, portal_delegate, connector, an
  unknown scope, a `user` scope carrying an agent identity, a loopback JWT with a
  vouched source agent, and an object with no `mcp_scope`. Here the ROUTE gate is
  what refuses.
* A real, signed loopback JWT with no source agent, through the real entry fence.

A person passes the gate and gets the handler's own answer on a fresh install
(no bindings, no workspace). That answer is named per route, so a pass is never
an incidental status. The other shape is VoIP with the feature flag off.
"""

from __future__ import annotations

import os
import secrets
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from db_harness import bootstrap_schema, run as _hrun  # noqa: E402

import dependencies  # noqa: E402
import routers.telegram as _TG  # noqa: E402
import routers.slack as _SL  # noqa: E402
import routers.voip as _VP  # noqa: E402
import routers.whatsapp as _WA  # noqa: E402
from database import db  # noqa: E402
from db_models import McpApiKeyCreate, UserCreate  # noqa: E402
from models import User  # noqa: E402

pytestmark = pytest.mark.unit

OWNER = "ch719-owner"
ADMIN = "ch719-admin"
AGENT = "ch719-agent"
SIB = "ch719-sib"  # an agent the same owner owns, which spawned AGENT
SYSTEM = "ch719-system"
GHOST = "ch719-no-such-agent"

HUMAN_ONLY_DETAIL = dependencies.HUMAN_ONLY_DETAIL
INTERACTIVE_KEY_DETAIL = (
    "This operation requires an interactive session; MCP API keys cannot perform it"
)
LOOPBACK_FENCE_DETAIL = "Event-loopback tokens may only dispatch a subscriber task"

ROUTERS = [_TG.auth_router, _WA.auth_router, _VP.auth_router, _SL.auth_router]


@dataclass(frozen=True)
class Route:
    """One gated route: its rule, a well-formed body, an optional schema-invalid
    body or query string, and the handler's own answer to a person on a fresh
    install."""

    method: str
    path: str  # "{a}" is the agent in the path
    rule: str  # "person" | "interactive"
    person_status: int
    person_json: dict  # a subset of the handler's JSON answer
    body: Optional[dict] = None
    invalid_body: Optional[dict] = None
    invalid_query: Optional[str] = None

    def url(self, agent: str = AGENT) -> str:
        return self.path.format(a=agent)


ROUTES = {
    # Telegram: binding a bot token binds a credential; the rest are a person's.
    "telegram.configure": Route(
        "PUT",
        "/api/agents/{a}/telegram",
        "interactive",
        400,
        {"detail": "Invalid bot token format. Expected format: 123456:ABC-DEF"},
        body={"bot_token": "not-a-bot-token"},
        invalid_body={},
    ),
    "telegram.delete": Route(
        "DELETE",
        "/api/agents/{a}/telegram",
        "person",
        404,
        {"detail": "No Telegram binding found"},
    ),
    "telegram.delete_group": Route(
        "DELETE",
        "/api/agents/{a}/telegram/groups/1",
        "person",
        404,
        {"detail": "No Telegram binding found"},
    ),
    "telegram.get": Route(
        "GET",
        "/api/agents/{a}/telegram",
        "person",
        200,
        {"configured": False, "webhook_url": None},
    ),
    "telegram.progress_indicator": Route(
        "PUT",
        "/api/agents/{a}/telegram/progress-indicator",
        "person",
        404,
        {"detail": "No Telegram binding found"},
        body={"enabled": True},
        invalid_body={},
    ),
    "telegram.test": Route(
        "POST",
        "/api/agents/{a}/telegram/test",
        "person",
        404,
        {"detail": "No Telegram binding found or token decryption failed"},
        body={},
        invalid_body={"chat_id": {"not": "a string"}},
    ),
    # WhatsApp: binding Twilio credentials binds a credential; the rest are a person's.
    "whatsapp.configure": Route(
        "PUT",
        "/api/agents/{a}/whatsapp",
        "interactive",
        400,
        {"detail": "AccountSid must start with 'AC' and be 34 characters long."},
        body={
            "account_sid": "XX-not-a-sid",
            "auth_token": "not-a-token",
            "from_number": "whatsapp:+14155238886",
        },
        invalid_body={},
    ),
    "whatsapp.delete": Route(
        "DELETE",
        "/api/agents/{a}/whatsapp",
        "person",
        404,
        {"detail": "No WhatsApp binding found"},
    ),
    "whatsapp.get": Route(
        "GET",
        "/api/agents/{a}/whatsapp",
        "person",
        200,
        {"configured": False, "webhook_url": None},
    ),
    "whatsapp.test": Route(
        "POST",
        "/api/agents/{a}/whatsapp/test",
        "person",
        404,
        {"detail": "No WhatsApp binding found"},
        body={},
        invalid_body={"to_number": {"not": "a string"}},
    ),
    # VoIP (feature flag on in the module world): binding Twilio credentials binds
    # a credential; removing the binding and the enable toggle are a person's.
    "voip.configure": Route(
        "PUT",
        "/api/agents/{a}/voip",
        "interactive",
        400,
        {"detail": "AccountSid must start with 'AC' and be 34 characters long."},
        body={
            "account_sid": "XX-not-a-sid",
            "auth_token": "not-a-token",
            "from_number": "+14155238886",
        },
        invalid_body={},
    ),
    "voip.delete": Route(
        "DELETE",
        "/api/agents/{a}/voip",
        "person",
        200,
        {"deleted": False, "agent_name": AGENT},
    ),
    "voip.enabled": Route(
        "PUT",
        "/api/agents/{a}/voip/enabled",
        "person",
        404,
        {"detail": "No VoIP binding configured for this agent"},
        body={"enabled": True},
        invalid_body={},
    ),
    # Slack: every write exposes the agent to a workspace or routes the
    # workspace's traffic to it, and the connection read names a person.
    "slack.connect": Route(
        "POST",
        "/api/agents/{a}/public-links/link-ch719/slack/connect",
        "person",
        404,
        {"detail": "Public link not found"},
    ),
    "slack.disconnect": Route(
        "DELETE",
        "/api/agents/{a}/public-links/link-ch719/slack",
        "person",
        404,
        {"detail": "Public link not found"},
    ),
    "slack.update_connection": Route(
        "PUT",
        "/api/agents/{a}/public-links/link-ch719/slack",
        "person",
        404,
        {"detail": "Public link not found"},
        invalid_query="enabled=not-a-bool",
    ),
    "slack.connection_status": Route(
        "GET",
        "/api/agents/{a}/public-links/link-ch719/slack",
        "person",
        404,
        {"detail": "Public link not found"},
    ),
    "slack.create_channel": Route(
        "POST",
        "/api/agents/{a}/slack/channel",
        "person",
        400,
        {
            "detail": "No Slack workspace connected. Install a workspace from Settings first."
        },
    ),
    "slack.delete_channel": Route(
        "DELETE",
        "/api/agents/{a}/slack/channel",
        "person",
        404,
        {"detail": "Agent is not bound to any Slack channel"},
    ),
    "slack.dm_default": Route(
        "PUT",
        "/api/agents/{a}/slack/channel/dm-default",
        "person",
        404,
        {"detail": "Agent is not bound to any Slack channel"},
    ),
    "slack.proactive": Route(
        "PUT",
        "/api/agents/{a}/slack/channels/C719/proactive",
        "person",
        404,
        {"detail": "Channel not found or not bound to this agent"},
        body={"allow_proactive": True},
        invalid_body={},
    ),
}

PERSON_ROUTES = [k for k, r in ROUTES.items() if r.rule == "person"]
INTERACTIVE_ROUTES = [k for k, r in ROUTES.items() if r.rule == "interactive"]
INVALID_ROUTES = [
    k
    for k, r in ROUTES.items()
    if r.invalid_body is not None or r.invalid_query is not None
]


def _expected_refusal(route: Route, scope) -> object:
    """The detail the route's own gate answers with, for a principal whose
    `mcp_scope` is `scope` ("key" for a real key row, "absent" for an object
    with no `mcp_scope` attribute at all).

    PERSON always answers `HUMAN_ONLY_DETAIL`. INTERACTIVE answers anything that
    is not a JWT (a key of any scope, or no `mcp_scope` at all) with
    `reject_non_interactive_principal`'s sentence, and a JWT carrying a vouched
    source agent (`mcp_scope` None) with `HUMAN_ONLY_DETAIL`."""
    if route.rule == "person" or scope is None:
        return HUMAN_ONLY_DETAIL
    return INTERACTIVE_KEY_DETAIL


def _send(client, route: Route, *, agent: str = AGENT, headers=None, body="default"):
    payload = route.body if body == "default" else body
    kwargs = {"headers": headers or {}}
    if payload is not None:
        kwargs["json"] = payload
    return client.request(route.method, route.url(agent), **kwargs)


def _assert_answer(res, status: int, subset: dict):
    assert res.status_code == status, res.text
    got = res.json()
    for key, value in subset.items():
        assert got.get(key) == value, (key, got)


# --------------------------------------------------------------------------- #
# The world: an owner, an admin, two owner-owned agents (SIB spawned AGENT)
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def _world(tmp_path_factory):
    """One fresh SQLite schema for the whole module, with every key minted once.

    Nothing here writes state: every refusal stops at the route gate, and every
    person path on a fresh install is a 404, a 400 or a read. So one world
    serves every case, and the gates under test read no database at all (the
    ownership lookup behind them does), so SQLite alone is enough."""
    from db.engine import dispose_engines

    mp = pytest.MonkeyPatch()
    mp.setenv("TRINITY_DB_PATH", str(tmp_path_factory.mktemp("ch719") / "trinity.db"))
    mp.delenv("DATABASE_URL", raising=False)
    dispose_engines()
    bootstrap_schema()
    mp.setattr(dependencies, "get_breaker_redis", lambda: None)
    mp.setenv("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))
    # VoIP on (VOIP_ENABLED + GEMINI_API_KEY in production); the other-shape
    # cases below turn it off per test.
    mp.setattr(_VP.voip_service, "is_available", lambda: True)

    db.create_user(
        UserCreate(username=OWNER, role="operator", email="ch719-owner@example.com")
    )
    db.create_user(
        UserCreate(username=ADMIN, role="admin", email="ch719-admin@example.com")
    )
    db.register_agent_owner(AGENT, OWNER)
    db.register_agent_owner(SIB, OWNER)
    owner_id = db.get_user_by_username(OWNER)["id"]

    # SIB's own key, recorded as the key that spawned AGENT, so AGENT is inside
    # SIB's manage reach (`dependencies._spawned_by_caller`).
    sib_key = db.create_agent_mcp_api_key(SIB, OWNER)
    _hrun(
        "UPDATE agent_ownership SET spawned_by_agent = :s, spawned_by_key_id = :k "
        "WHERE agent_name = :a",
        s=SIB,
        k=sib_key.id,
        a=AGENT,
    )
    # The system key, made the way the platform makes it: an agent key under
    # the admin, then its scope column set to `system`.
    system_key = db.create_agent_mcp_api_key(SYSTEM, ADMIN)
    _hrun("UPDATE mcp_api_keys SET scope = 'system' WHERE id = :i", i=system_key.id)
    keys = {
        "agent-own-key": _auth(db.create_agent_mcp_api_key(AGENT, OWNER).api_key),
        "agent-sibling-in-reach": _auth(sib_key.api_key),
        "system-key": _auth(system_key.api_key),
        "owner-user-key": _auth(
            db.create_mcp_api_key(OWNER, McpApiKeyCreate(name="owner key")).api_key
        ),
    }

    app = FastAPI()
    for router in ROUTERS:
        app.include_router(router)
    client = TestClient(app)

    def as_(**fields):
        base = dict(
            id=owner_id,
            username=OWNER,
            email="ch719-owner@example.com",
            role="operator",
        )
        base.update(fields)
        app.dependency_overrides[dependencies.get_current_user] = lambda: User(**base)

    try:
        yield SimpleNamespace(
            client=client, app=app, as_=as_, keys=keys, owner_id=owner_id
        )
    finally:
        app.dependency_overrides.clear()
        mp.undo()
        dispose_engines()


@pytest.fixture
def world(_world):
    """The module world, with no principal override left from the last case."""
    _world.app.dependency_overrides.clear()
    yield _world
    _world.app.dependency_overrides.clear()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _jwt(username: str) -> dict:
    return _auth(dependencies.create_access_token({"sub": username}))


MACHINE_KEYS = ["agent-own-key", "agent-sibling-in-reach", "system-key"]

# (fields for the override, does the principal carry an mcp_scope of None?)
OVERRIDE_PRINCIPALS = {
    "ops-key": {"mcp_scope": "ops"},
    "portal-delegate-key": {"mcp_scope": "portal_delegate", "portal_delegate": True},
    "connector-key": {"mcp_scope": "connector", "connector_agent": AGENT},
    "unknown-future-scope": {"mcp_scope": "a-scope-shipped-tomorrow"},
    "user-scope-carrying-an-agent": {"mcp_scope": "user", "agent_name": AGENT},
    "vouched-loopback-jwt": {
        "mcp_scope": None,
        "is_event_loopback": True,
        "vouched_source_agent": SIB,
    },
}


class _NoScopeAttribute:
    """The owner, with NO `mcp_scope` attribute at all. Both gates must fail
    closed on it (a sentinel, never `getattr(..., None)`)."""

    username = OWNER
    email = "ch719-owner@example.com"
    role = "operator"
    agent_name = None
    connector_agent = None
    portal_delegate = False

    def __init__(self, uid):
        self.id = uid


# --------------------------------------------------------------------------- #
# Machine principals are refused, through the real authentication path
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", MACHINE_KEYS)
@pytest.mark.parametrize("route_id", list(ROUTES))
def test_a_machine_key_is_refused(world, route_id, key):
    route = ROUTES[route_id]
    res = _send(world.client, route, headers=world.keys[key])
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == _expected_refusal(route, scope="key"), res.text


@pytest.mark.parametrize("route_id", INTERACTIVE_ROUTES)
def test_binding_a_credential_refuses_the_owners_user_key(world, route_id):
    """INTERACTIVE: the person's own `user` key is a credential too, and a
    credential bind must be stricter than the class of key it could mint."""
    route = ROUTES[route_id]
    res = _send(world.client, route, headers=world.keys["owner-user-key"])
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == INTERACTIVE_KEY_DETAIL


@pytest.mark.parametrize("principal", list(OVERRIDE_PRINCIPALS))
@pytest.mark.parametrize("route_id", list(ROUTES))
def test_every_other_principal_is_refused_by_the_route_gate(world, route_id, principal):
    route = ROUTES[route_id]
    fields = OVERRIDE_PRINCIPALS[principal]
    world.as_(**fields)
    res = _send(world.client, route)
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == _expected_refusal(
        route, scope=fields["mcp_scope"]
    ), res.text


@pytest.mark.parametrize("route_id", list(ROUTES))
def test_a_principal_without_mcp_scope_fails_closed(world, route_id):
    route = ROUTES[route_id]
    uid = db.get_user_by_username(OWNER)["id"]
    world.app.dependency_overrides[dependencies.get_current_user] = (
        lambda: _NoScopeAttribute(uid)
    )
    res = _send(world.client, route)
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == _expected_refusal(route, scope="absent"), res.text


@pytest.mark.parametrize("route_id", list(ROUTES))
def test_an_unvouched_loopback_jwt_is_refused_by_the_entry_fence(world, route_id):
    """A real, signed loopback token with no source agent, through the real
    `get_current_user`: the entry fence refuses it on every route but the
    subscriber-task dispatch. The route gates need not, and this pins that layer
    on these routes."""
    from jose import jwt

    token = jwt.encode(
        {
            "sub": ADMIN,
            "scope": dependencies.EVENT_LOOPBACK_SCOPE,
            "exp": int(time.time()) + 300,
        },
        dependencies.SECRET_KEY,
        algorithm=dependencies.ALGORITHM,
    )
    res = _send(world.client, ROUTES[route_id], headers=_auth(token))
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == LOOPBACK_FENCE_DETAIL


# --------------------------------------------------------------------------- #
# Ordering and bodies: the refusal comes first and depends on the principal only
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", ["agent-own-key", "system-key"])
@pytest.mark.parametrize("route_id", list(ROUTES))
def test_a_nonexistent_agent_gets_the_same_refusal(world, route_id, key):
    route = ROUTES[route_id]
    res = _send(world.client, route, agent=GHOST, headers=world.keys[key])
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == _expected_refusal(route, scope="key"), res.text


def _send_invalid(client, route: Route, headers: dict):
    if route.invalid_query is not None:
        return client.request(
            route.method, f"{route.url()}?{route.invalid_query}", headers=headers
        )
    return _send(client, route, headers=headers, body=route.invalid_body)


@pytest.mark.parametrize("key", ["agent-own-key", "system-key"])
@pytest.mark.parametrize("route_id", INVALID_ROUTES)
def test_a_schema_invalid_request_is_refused_before_validation(world, route_id, key):
    route = ROUTES[route_id]
    res = _send_invalid(world.client, route, world.keys[key])
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == _expected_refusal(route, scope="key"), res.text


@pytest.mark.parametrize("route_id", INVALID_ROUTES)
def test_the_invalid_request_really_is_invalid(world, route_id):
    """Control: the same request from a person is a 422, so the 403 above is
    the gate answering first, not a request that happened to validate."""
    route = ROUTES[route_id]
    res = _send_invalid(world.client, route, _jwt(OWNER))
    assert res.status_code == 422, res.text


# --------------------------------------------------------------------------- #
# A person passes the gate and gets the handler's own answer
# --------------------------------------------------------------------------- #

PERSONS = {
    "owner-jwt": lambda: _jwt(OWNER),
    "admin-jwt-not-owner": lambda: _jwt(ADMIN),
}


@pytest.mark.parametrize("who", list(PERSONS))
@pytest.mark.parametrize("route_id", list(ROUTES))
def test_a_signed_in_person_passes(world, route_id, who):
    route = ROUTES[route_id]
    res = _send(world.client, route, headers=PERSONS[who]())
    _assert_answer(res, route.person_status, route.person_json)


@pytest.mark.parametrize("route_id", PERSON_ROUTES)
def test_the_owners_user_key_passes_a_person_route(world, route_id):
    route = ROUTES[route_id]
    res = _send(world.client, route, headers=world.keys["owner-user-key"])
    _assert_answer(res, route.person_status, route.person_json)


def test_unauthenticated_stays_401(world):
    res = _send(world.client, ROUTES["telegram.get"])
    assert res.status_code == 401, res.text


# --------------------------------------------------------------------------- #
# The other shape: VoIP with the feature flag off
# --------------------------------------------------------------------------- #

VOIP_ROUTES = [k for k in ROUTES if k.startswith("voip.")]
VOIP_DISABLED = {"detail": "VoIP is not enabled"}


@pytest.fixture
def voip_off(world, monkeypatch):
    monkeypatch.setattr(_VP.voip_service, "is_available", lambda: False)
    return world


@pytest.mark.parametrize("route_id", VOIP_ROUTES)
def test_voip_off_a_person_gets_the_disabled_answer(voip_off, route_id):
    res = _send(voip_off.client, ROUTES[route_id], headers=_jwt(OWNER))
    _assert_answer(res, 404, VOIP_DISABLED)


@pytest.mark.parametrize("key", MACHINE_KEYS)
@pytest.mark.parametrize("route_id", VOIP_ROUTES)
def test_voip_off_a_machine_key_is_refused_before_the_flag_is_read(
    voip_off, route_id, key
):
    route = ROUTES[route_id]
    res = _send(voip_off.client, route, headers=voip_off.keys[key])
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == _expected_refusal(route, scope="key"), res.text


@pytest.mark.parametrize("route_id", VOIP_ROUTES)
def test_voip_off_the_owners_user_key(voip_off, route_id):
    """The user key is refused first on the credential bind and gets the
    disabled answer on the person routes."""
    route = ROUTES[route_id]
    res = _send(voip_off.client, route, headers=voip_off.keys["owner-user-key"])
    if route.rule == "interactive":
        assert res.status_code == 403, res.text
        assert res.json()["detail"] == INTERACTIVE_KEY_DETAIL
    else:
        _assert_answer(res, 404, VOIP_DISABLED)
