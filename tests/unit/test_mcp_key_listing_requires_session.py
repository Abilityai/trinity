"""Listing MCP keys requires a signed-in session.

`GET /api/mcp/keys` is the key inventory the Settings → MCP Keys tab renders.
It has no machine consumer (the CLI and the MCP server never call it with a
key), so it takes the INTERACTIVE rule, `Depends(require_interactive)` like its
POST siblings — a JWT session only. Every MCP key, the person's
own `user` key included, gets a 403. A signed-in admin still sees the full
listing with each key's `user_email` (the tab renders it on every row); a
signed-in non-admin sees only their own keys.

Key-authenticated callers go through the REAL `get_current_user` with a key row
seeded in the real schema (`tests/db_harness.py`). Kinds the entry point fences
earlier, or that cannot be seeded as a row, go through a dependency override
with a stated principal — every `models.User` field set, `mcp_scope` included,
never a `MagicMock` — so the route's own gate is what refuses them.

Related: docs/memory/feature-flows/mcp-api-keys.md.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi import FastAPI  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from db_harness import db_backend, run as _hrun  # noqa: E402,F401

import dependencies  # noqa: E402
import routers.mcp_keys as _KEYS  # noqa: E402
from database import db  # noqa: E402
from db_models import McpApiKeyCreate, UserCreate  # noqa: E402

pytestmark = pytest.mark.unit

ADMIN = "keylist-admin"
PLAIN = "keylist-plain"
AGENT = "keylist-agent"
SYSTEM = "keylist-system"
ADMIN_EMAIL = "keylist-admin@example.com"
PLAIN_EMAIL = "keylist-plain@example.com"

LIST = "/api/mcp/keys"


@dataclass
class _Principal:
    """Every field `models.User` carries, stated — never a MagicMock."""

    id: int
    username: str
    email: Optional[str]
    role: str
    agent_name: Optional[str] = None
    connector_agent: Optional[str] = None
    portal_delegate: bool = False
    mcp_scope: Optional[str] = None
    mcp_key_id: Optional[str] = None
    mcp_key_name: Optional[str] = None
    vouched_source_agent: Optional[str] = None


@dataclass
class _PrincipalWithoutScope:
    """Deliberately lacks `mcp_scope`: a stand-in that does not match the real
    principal must fail closed, never read as the JWT value."""

    id: int
    username: str
    email: Optional[str]
    role: str
    agent_name: Optional[str] = None
    connector_agent: Optional[str] = None
    portal_delegate: bool = False
    vouched_source_agent: Optional[str] = None


@pytest.fixture
def world(db_backend, monkeypatch):
    # JWT revocation is a Redis read; no Redis here.
    monkeypatch.setattr(dependencies, "get_breaker_redis", lambda: None)
    db.create_user(UserCreate(username=ADMIN, role="admin", email=ADMIN_EMAIL))
    db.create_user(UserCreate(username=PLAIN, role="operator", email=PLAIN_EMAIL))
    db.register_agent_owner(AGENT, ADMIN)
    # One key per account, so "full listing" and "own only" are distinguishable.
    db.create_mcp_api_key(ADMIN, McpApiKeyCreate(name="admin laptop"))
    db.create_mcp_api_key(PLAIN, McpApiKeyCreate(name="plain laptop"))
    app = FastAPI()
    app.include_router(_KEYS.router)
    yield app, TestClient(app)
    app.dependency_overrides.clear()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _jwt(username: str) -> dict:
    return _auth(dependencies.create_access_token({"sub": username}))


def _agent_key() -> dict:
    return _auth(db.create_agent_mcp_api_key(AGENT, ADMIN).api_key)


def _rescoped_key(scope: str, agent_name: str) -> dict:
    """A key row carrying `scope`, created the way the platform creates the
    system and connector keys: an agent key, then its scope column set."""
    key = db.create_agent_mcp_api_key(agent_name, ADMIN)
    _hrun("UPDATE mcp_api_keys SET scope = :s WHERE id = :i", s=scope, i=key.id)
    return _auth(key.api_key)


def _admin_user_key() -> dict:
    return _auth(
        db.create_mcp_api_key(ADMIN, McpApiKeyCreate(name="admin own key")).api_key
    )


REAL_KEY_KINDS = {
    "agent-key": _agent_key,
    "system-key": lambda: _rescoped_key("system", SYSTEM),
    "connector-key": lambda: _rescoped_key("connector", AGENT),
    "user-key": _admin_user_key,
}


def _admin_principal(**fields):
    base = {"id": 1, "username": ADMIN, "email": ADMIN_EMAIL, "role": "admin"}
    base.update(fields)
    return _Principal(**base)


OVERRIDE_KINDS = {
    # The real connector key is refused earlier, at the entry point; this one
    # reaches the route, so the route's own rule is what refuses it.
    "connector-principal": lambda: _admin_principal(
        mcp_scope="connector", connector_agent=AGENT
    ),
    # A JWT that the event loopback vouched for an agent is not a person's session.
    "loopback-jwt": lambda: _admin_principal(mcp_scope=None, vouched_source_agent=AGENT),
    "ops-key": lambda: _admin_principal(mcp_scope="ops"),
    "portal-delegate-key": lambda: _admin_principal(
        mcp_scope="portal_delegate", portal_delegate=True
    ),
    "unknown-future-scope": lambda: _admin_principal(mcp_scope="a-scope-shipped-tomorrow"),
    "missing-mcp-scope": lambda: _PrincipalWithoutScope(
        id=1, username=ADMIN, email=ADMIN_EMAIL, role="admin"
    ),
}


class TestKeyListingRequiresSession:
    @pytest.mark.parametrize("kind", sorted(REAL_KEY_KINDS))
    def test_key_listing_requires_session(self, world, kind):
        _, client = world
        res = client.get(LIST, headers=REAL_KEY_KINDS[kind]())
        assert res.status_code == 403, res.text

    @pytest.mark.parametrize("kind", sorted(OVERRIDE_KINDS))
    def test_refused_by_the_route_gate(self, world, kind):
        app, client = world
        principal = OVERRIDE_KINDS[kind]()
        app.dependency_overrides[dependencies.get_current_user] = lambda: principal
        res = client.get(LIST)
        assert res.status_code == 403, res.text


class TestSessionListingUnchanged:
    def test_a_signed_in_admin_sees_every_key_with_its_owner_email(self, world):
        _, client = world
        res = client.get(LIST, headers=_jwt(ADMIN))
        assert res.status_code == 200, res.text
        rows = {r["name"]: r for r in res.json()}
        assert {"admin laptop", "plain laptop"} <= set(rows)
        assert rows["plain laptop"]["user_email"] == PLAIN_EMAIL
        assert "api_key" not in rows["plain laptop"]  # never the secret

    def test_a_signed_in_user_sees_only_their_own_keys(self, world):
        _, client = world
        res = client.get(LIST, headers=_jwt(PLAIN))
        assert res.status_code == 200, res.text
        assert [r["name"] for r in res.json()] == ["plain laptop"]


# Every route in routers/mcp_keys.py, read off the router object. A route added
# to the file fails here until it is listed with the rule it follows and a test
# that pins that rule.
MCP_KEY_ROUTES = {
    ("POST", "/api/mcp/keys"): "interactive — test_mcp_key_creation_requires_session.py",
    ("GET", "/api/mcp/keys"): "interactive — this file",
    ("POST", "/api/mcp/keys/ensure-default"): "interactive — test_mcp_key_creation_requires_session.py",
    ("GET", "/api/mcp/keys/{key_id}"): "owner-scoped read — this file",
    ("POST", "/api/mcp/keys/{key_id}/revoke"): "agent + connector refused — test_1854_agent_mcp_key.py",
    ("DELETE", "/api/mcp/keys/{key_id}"): "agent + connector refused — test_1854_agent_mcp_key.py",
    ("POST", "/api/mcp/validate"): "unauthenticated by design: validates the presented key itself",
}


def test_every_route_in_the_file_is_listed_with_its_rule():
    live = {
        (method, route.path)
        for route in _KEYS.router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    assert live == set(MCP_KEY_ROUTES), (
        "routers/mcp_keys.py routes changed — list each with its rule: "
        f"added {sorted(live - set(MCP_KEY_ROUTES))}, removed {sorted(set(MCP_KEY_ROUTES) - live)}"
    )


def test_a_single_key_read_stays_owner_scoped(world):
    _, client = world
    other = db.create_mcp_api_key(ADMIN, McpApiKeyCreate(name="admin second"))
    res = client.get(f"{LIST}/{other.id}", headers=_jwt(PLAIN))
    assert res.status_code == 404, res.text


def test_validate_needs_no_session(world):
    _, client = world
    res = client.post("/api/mcp/validate")
    assert res.status_code == 401  # the missing key, not a missing session
