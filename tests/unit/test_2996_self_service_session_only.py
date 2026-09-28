"""The sign-in email and the personal GitHub PAT change only in a signed-in session.

`PUT /api/users/me/email` binds the account's sign-in identity
(trinity-enterprise#711), and `PUT` / `DELETE /api/users/me/github-pat` set the
credential future agent creations inherit (ent#162). All three take
`Depends(require_interactive)`: a JWT session only. Every MCP key — agent,
system, and the person's own `user` key — gets a 403 and the stored value does
not move.

The machine keys go through the REAL `get_current_user` with key rows seeded in
the real schema; the kinds the entry point fences earlier or that cannot be
minted as a row go through an override with a stated principal, so the route's
own gate is what refuses them.

The last class records how email sign-in resolves an account — by the email
column alone — which is why binding the sign-in email is a session-only act.

Related: docs/memory/requirements/auth.md §2.8,
feature-flows/email-authentication.md.
"""

from __future__ import annotations

import os
import sys
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

from db_harness import db_backend, run as _hrun, scalar as _hscalar  # noqa: E402,F401

import dependencies  # noqa: E402
import routers.users as _USERS  # noqa: E402
import services.github_service as _GH  # noqa: E402
from database import db  # noqa: E402
from db_models import McpApiKeyCreate, UserCreate  # noqa: E402

pytestmark = pytest.mark.unit

OWNER = "self2996-owner"
ADMIN = "self2996-admin"
AGENT = "self2996-agent"
SYSTEM = "self2996-system"
OWNER_EMAIL = (
    "self2996-owner@example.com"  # non-ambient: every request asks for another
)
NEW_EMAIL = "self2996-new@example.com"
_ENC_KEY = "ab" * 32


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


@pytest.fixture
def world(db_backend, monkeypatch):
    monkeypatch.setattr(dependencies, "get_breaker_redis", lambda: None)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", _ENC_KEY)

    async def _valid(self):
        return "valid", "octocat"

    monkeypatch.setattr(_GH.GitHubService, "validate_token_detailed", _valid)
    db.create_user(UserCreate(username=OWNER, role="admin", email=OWNER_EMAIL))
    db.create_user(
        UserCreate(username=ADMIN, role="admin", email="self2996-admin@example.com")
    )
    db.register_agent_owner(AGENT, OWNER)
    owner_id = db.get_user_by_username(OWNER)["id"]
    assert db.set_user_github_pat(owner_id, "ghp_seededSeededSeeded0001")

    app = FastAPI()
    app.include_router(_USERS.router)

    def as_(**fields):
        base = {
            "id": owner_id,
            "username": OWNER,
            "email": OWNER_EMAIL,
            "role": "admin",
        }
        base.update(fields)
        app.dependency_overrides[dependencies.get_current_user] = lambda: _Principal(
            **base
        )

    yield SimpleNamespace(client=TestClient(app), app=app, as_=as_, owner_id=owner_id)
    app.dependency_overrides.clear()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _jwt(username: str = OWNER) -> dict:
    return _auth(dependencies.create_access_token({"sub": username}))


def _agent_key() -> dict:
    return _auth(db.create_agent_mcp_api_key(AGENT, OWNER).api_key)


def _system_key() -> dict:
    key = db.create_agent_mcp_api_key(SYSTEM, OWNER)
    _hrun("UPDATE mcp_api_keys SET scope = 'system' WHERE id = :i", i=key.id)
    return _auth(key.api_key)


def _owner_user_key() -> dict:
    return _auth(
        db.create_mcp_api_key(OWNER, McpApiKeyCreate(name="owner key")).api_key
    )


KEY_KINDS = [
    pytest.param(_agent_key, id="agent-key"),
    pytest.param(_system_key, id="system-key"),
    pytest.param(_owner_user_key, id="owner-user-key"),
]

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
    pytest.param({"vouched_source_agent": AGENT}, id="loopback-jwt"),
]


def _stored_email() -> str:
    return _hscalar("SELECT email FROM users WHERE username = :u", u=OWNER)


def _stored_pat() -> Optional[str]:
    return _hscalar(
        "SELECT github_pat_encrypted FROM users WHERE username = :u", u=OWNER
    )


# (method, path, json body)
WRITES = {
    "email": ("PUT", "/api/users/me/email", {"email": NEW_EMAIL}),
    "github-pat-set": (
        "PUT",
        "/api/users/me/github-pat",
        {"pat": "ghp_replacementReplacement02"},
    ),
    "github-pat-clear": ("DELETE", "/api/users/me/github-pat", None),
}


def _call(world, name, headers=None):
    method, path, body = WRITES[name]
    return world.client.request(method, path, json=body, headers=headers or {})


def _snapshot():
    email, pat = _stored_email(), _stored_pat()
    assert email == OWNER_EMAIL and pat, "seed missing"
    return email, pat


class TestKeysAreRefused:
    @pytest.mark.parametrize("name", sorted(WRITES))
    @pytest.mark.parametrize("key", KEY_KINDS)
    def test_a_key_is_refused_and_nothing_is_stored(self, world, name, key):
        before = _snapshot()
        res = _call(world, name, key())
        assert res.status_code == 403, res.text
        assert (_stored_email(), _stored_pat()) == before

    @pytest.mark.parametrize("name", sorted(WRITES))
    @pytest.mark.parametrize("principal", OVERRIDE_REFUSED)
    def test_refused_by_the_route_gate(self, world, name, principal):
        before = _snapshot()
        world.as_(**principal)
        res = _call(world, name)
        assert res.status_code == 403, res.text
        assert (_stored_email(), _stored_pat()) == before


class TestSessionPathUnchanged:
    def test_a_session_binds_a_new_email(self, world):
        res = _call(world, "email", _jwt())
        assert res.status_code == 200, res.text
        assert _stored_email() == NEW_EMAIL

    def test_a_malformed_email_is_still_400(self, world):
        res = world.client.put(
            "/api/users/me/email", json={"email": "not-an-email"}, headers=_jwt()
        )
        assert res.status_code == 400, res.text
        assert _stored_email() == OWNER_EMAIL

    def test_another_accounts_email_is_still_409(self, world):
        res = world.client.put(
            "/api/users/me/email",
            json={"email": "self2996-admin@example.com"},
            headers=_jwt(),
        )
        assert res.status_code == 409, res.text
        assert _stored_email() == OWNER_EMAIL

    def test_a_session_replaces_the_pat(self, world):
        before = _stored_pat()
        res = _call(world, "github-pat-set", _jwt())
        assert res.status_code == 200, res.text
        after = _stored_pat()
        assert after and after != before

    def test_a_session_clears_the_pat(self, world):
        res = _call(world, "github-pat-clear", _jwt())
        assert res.status_code == 200, res.text
        assert not _stored_pat()
        assert db.has_user_github_pat(world.owner_id) is False

    def test_the_pat_status_read_is_unchanged(self, world):
        """GET /me/github-pat returns a flag, never the token — left as it was."""
        res = world.client.get("/api/users/me/github-pat", headers=_owner_user_key())
        assert res.status_code == 200, res.text
        assert res.json()["configured"] is True


class TestEmailSignInResolvesByEmailAlone:
    """Email sign-in finds the account by `users.email`, nothing else
    (`db/email_auth.py::get_or_create_email_user`). So the account a mailbox
    signs in to is whichever row carries that address — which is why binding
    the sign-in email is a session-only act."""

    def test_after_a_rebind_the_old_address_resolves_to_a_new_account(self, world):
        db.add_to_whitelist(
            OWNER_EMAIL, added_by=ADMIN, source="manual", default_role="user"
        )
        assert _call(world, "email", _jwt()).status_code == 200

        resolved = db.get_or_create_email_user(OWNER_EMAIL)
        assert resolved["username"] == OWNER_EMAIL
        assert resolved["id"] != world.owner_id
        assert _stored_email() == NEW_EMAIL

    def test_after_a_rebind_an_email_named_account_cannot_be_recreated(self, world):
        """An account whose username IS its address: the new row collides with
        it on the username key, so the lookup raises rather than resolving."""
        db.create_user(
            UserCreate(
                username="self2996-r@example.com",
                role="user",
                email="self2996-r@example.com",
            )
        )
        _hrun(
            "UPDATE users SET email = :e WHERE username = :u",
            e="self2996-elsewhere@example.com",
            u="self2996-r@example.com",
        )
        with pytest.raises(Exception):
            db.get_or_create_email_user("self2996-r@example.com")
