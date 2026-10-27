"""trinity-enterprise#717 — adding a login-whitelist entry takes a signed-in admin session.

`POST /api/settings/email-whitelist` pre-authorises an address at a role. The
person's first email sign-in then yields an **interactive session** at that
role, which passes every INTERACTIVE route and outlives any key. The route was
gated by `assert_admin` alone, and the admin gate admits an admin's own
`user`-scoped key and the `system` key by design (#2323), so a key could mint a
principal stronger than itself — up to an interactive admin.

The rule (#2996): a minter is at least as strict as the principal it produces.
Every whitelist role yields an interactive session (and `operator` and above can
mint keys), so the route takes `Depends(require_interactive)` for EVERY role,
the default `user` included, and keeps the handler's `assert_admin`. Both run
before the body is read: a refused caller gets 403, never a validation 422, and
no row is written or replaced.

Two enforcement layers, each tested where it lives:

* the route gate (`require_interactive`) — driven with resolved principals,
  `get_current_user` overridden: every key scope, an unknown scope, a principal
  with no `mcp_scope` at all, and a loopback JWT carrying a vouched source agent;
* the `get_current_user` entry fence — driven with a real, signed loopback JWT
  that carries NO source agent (the backend mints that shape too), with no
  override, because the route primitive does not and need not refuse it.

Everything goes through the real composed settings router (`routers.settings`,
so the `/{key}` catch-all and every sibling are in play — Invariant #4). The
whitelist module's `db` is replaced by a recorder; the replacement rules it would
apply are pinned by `test_837_whitelist_grant.py` against the real DB layer.

Sync throughout (`tests/unit/pytest.ini` runs pytest-asyncio in strict mode).
"""
import time
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.settings as rs  # the composed router
# #1028: collaborators are patched on the module that owns them.
import routers.settings.whitelist as rs_whitelist
from models import User

pytestmark = pytest.mark.unit

URL = "/api/settings/email-whitelist"

# The leaf the route authenticates through, taken from the module that mounts
# it, so the override and the patches below hit the objects the app really uses.
get_current_user = rs_whitelist.get_current_user
_DEPS = get_current_user.__globals__  # the `dependencies` module's namespace
HUMAN_ONLY_DETAIL = _DEPS["HUMAN_ONLY_DETAIL"]
EVENT_LOOPBACK_SCOPE = _DEPS["EVENT_LOOPBACK_SCOPE"]
LOOPBACK_FENCE_DETAIL = "Event-loopback tokens may only dispatch a subscriber task"

# Every whitelist role, plus "no role in the body" (the model defaults it to user).
ROLES = ["user", "operator", "creator", "admin", None]


def _admin(**kw):
    base = dict(id=1, username="admin", email="admin@example.com", role="admin")
    base.update(kw)
    return User(**base)


class _NoScopeAttribute:
    """An admin principal with NO `mcp_scope` attribute at all.

    `is_interactive_principal` must fail closed on it (sentinel, never
    `getattr(..., None)`, which would make the absent attribute the JWT value).
    """

    id = 1
    username = "admin"
    email = "admin@example.com"
    role = "admin"
    agent_name = None
    connector_agent = None
    portal_delegate = False


# Resolved admin principals for each credential class the INTERACTIVE rule
# refuses. `user` and `system` are the two the admin gate admits (#2323) — the
# defect; the rest pin the allowlist's fail-closed edges.
KEYS = {
    "user": lambda: _admin(mcp_scope="user"),
    "system": lambda: _admin(mcp_scope="system"),
    "agent": lambda: _admin(mcp_scope="agent", agent_name="agent-a"),
    "connector": lambda: _admin(mcp_scope="connector", connector_agent="agent-a"),
    "ops": lambda: _admin(mcp_scope="ops"),
    "portal_delegate": lambda: _admin(mcp_scope="portal_delegate", portal_delegate=True),
    "unknown": lambda: _admin(mcp_scope="a-scope-no-one-has-shipped"),
    "absent": lambda: _NoScopeAttribute(),
}

# Bodies a signed-in admin would get a 422 for. A refused caller must get 403
# for every one of them: answering 422 would mean the body was parsed first.
BAD_BODIES = {
    "empty_object": b"{}",
    "invalid_email": b'{"email": "a@"}',
    "json_array": b'[{"email": "new@example.com"}]',
    "malformed_json": b"{not json",
}


class _Whitelist:
    """Records every call the route makes on its `db` collaborator.

    Answers as for an empty table with no accounts. Any accessor the route was
    not expected to use fails loudly instead of returning a quiet default.
    """

    def __init__(self):
        self.calls = []

    def add_to_whitelist(self, email, added_by, source="manual", default_role="user"):
        self.calls.append(("add_to_whitelist", email, added_by, source, default_role))
        return True

    def get_user_by_email(self, email):
        self.calls.append(("get_user_by_email", email))
        return None

    def get_whitelist_default_role(self, email):
        self.calls.append(("get_whitelist_default_role", email))
        return None

    def __getattr__(self, name):
        raise AssertionError(f"unexpected whitelist db accessor: {name}")


def _counting_client(app):
    """A TestClient over `app` that counts the request-body messages it consumes."""
    reads = {"n": 0}

    async def counting(scope, receive, send):
        async def counted_receive():
            message = await receive()
            if message.get("type") == "http.request":
                reads["n"] += 1
            return message

        await app(scope, counted_receive, send)

    return TestClient(counting), reads


def _mount():
    app = FastAPI()
    app.include_router(rs.router)
    return app


@pytest.fixture
def whitelist_db(monkeypatch):
    recorder = _Whitelist()
    monkeypatch.setattr(rs_whitelist, "db", recorder)
    return recorder


@pytest.fixture
def env(whitelist_db):
    """The real router with the authentication leaf answering `env.principal`."""
    app = _mount()
    holder = {}
    app.dependency_overrides[get_current_user] = lambda: holder["principal"]
    client, reads = _counting_client(app)
    yield types.SimpleNamespace(client=client, reads=reads, db=whitelist_db, holder=holder)
    app.dependency_overrides.clear()


def _body(role, **extra):
    body = {"email": "new@example.com", **extra}
    if role is not None:
        body["default_role"] = role
    return body


def _assert_refused_untouched(env, response):
    assert response.status_code == 403, response.text
    assert env.reads["n"] == 0, "the body was read before the refusal"
    assert env.db.calls == [], f"the whitelist was touched: {env.db.calls}"


# --------------------------------------------------------------------------- #
# The route gate: no key mints an interactive sign-in, at any role
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("role", ROLES, ids=str)
@pytest.mark.parametrize("key", list(KEYS))
def test_keys_cannot_add(env, key, role):
    env.holder["principal"] = KEYS[key]()
    response = env.client.post(URL, json=_body(role))
    _assert_refused_untouched(env, response)


@pytest.mark.parametrize("body", list(BAD_BODIES))
@pytest.mark.parametrize("key", list(KEYS))
def test_keys_are_refused_before_body_read(env, key, body):
    env.holder["principal"] = KEYS[key]()
    response = env.client.post(
        URL, content=BAD_BODIES[body], headers={"content-type": "application/json"}
    )
    _assert_refused_untouched(env, response)


@pytest.mark.parametrize("role", ROLES, ids=str)
def test_loopback_is_refused_by_the_route_gate(env, role):
    """A loopback JWT carrying a vouched source agent: the route's second layer.

    Authentication is overridden, so the entry fence is out of the picture and
    the refusal can only come from the route's own gate. `mcp_scope=None` and
    `role=admin` are exactly what the loopback resolves to, so the admin gate
    alone admits it.
    """
    env.holder["principal"] = _admin(
        mcp_scope=None, is_event_loopback=True, vouched_source_agent="agent-a"
    )
    response = env.client.post(URL, json=_body(role))
    _assert_refused_untouched(env, response)
    assert response.json()["detail"] == HUMAN_ONLY_DETAIL


def test_unvouched_loopback_is_refused_by_the_entry_fence(whitelist_db, monkeypatch):
    """A real, signed loopback JWT with NO source agent, through real authentication.

    The route primitive does not refuse this shape (it carries no
    `vouched_source_agent`); the `get_current_user` entry fence does, on every
    route but the subscriber-task dispatch. This pins that layer on THIS route.
    """
    from config import ALGORITHM, SECRET_KEY
    from jose import jwt

    # Only reached if the fence let the token through: an admin account that is
    # not suspended and a token that is not revoked, so a pass would be a 200,
    # not an incidental 401.
    monkeypatch.setattr(_DEPS["db"], "get_user_by_username", lambda *_a, **_k: {
        "id": 1, "username": "admin", "email": "admin@example.com", "role": "admin",
        "suspended_at": None,
    })
    monkeypatch.setitem(_DEPS, "is_token_revoked", lambda *_a, **_k: False)
    token = jwt.encode(
        {"sub": "admin", "scope": EVENT_LOOPBACK_SCOPE, "exp": int(time.time()) + 300},
        SECRET_KEY, algorithm=ALGORITHM,
    )

    client, reads = _counting_client(_mount())  # no dependency override
    response = client.post(
        URL, json=_body("admin"), headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == LOOPBACK_FENCE_DETAIL
    assert reads["n"] == 0
    assert whitelist_db.calls == []


# --------------------------------------------------------------------------- #
# Controls: the people who may add still can, and nobody else gets in
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("role", ROLES, ids=str)
def test_jwt_admin_can_add(env, role):
    """A signed-in admin is unchanged at every role (guards a deny-everyone gate).

    The email is normalised by the model; an explicit `source` is forwarded and
    an omitted one defaults to `manual`; an omitted role defaults to `user`.
    """
    env.holder["principal"] = _admin(mcp_scope=None)
    extra = {"source": "access_request"} if role is not None else {}
    response = env.client.post(URL, json={**_body(role, **extra), "email": " New@Example.COM "})

    assert response.status_code == 200, response.text
    assert response.json() == {"success": True, "email": "new@example.com"}
    assert env.db.calls[0] == (
        "add_to_whitelist",
        "new@example.com",
        "admin",
        "access_request" if role is not None else "manual",
        role or "user",
    )


def test_non_admin_session_is_refused_before_body_read(env):
    """A signed-in creator passes INTERACTIVE and must still meet `assert_admin`.

    Without it, any signed-in creator could whitelist an address as admin.
    """
    env.holder["principal"] = _admin(mcp_scope=None, role="creator", username="creator")
    response = env.client.post(
        URL, content=BAD_BODIES["malformed_json"], headers={"content-type": "application/json"}
    )
    _assert_refused_untouched(env, response)


def test_unauthenticated_request_remains_401(whitelist_db):
    client, reads = _counting_client(_mount())  # no dependency override
    response = client.post(URL, json=_body("admin"))

    assert response.status_code == 401, response.text
    assert reads["n"] == 0
    assert whitelist_db.calls == []
