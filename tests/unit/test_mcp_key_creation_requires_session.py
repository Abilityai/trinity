"""Creating an MCP key requires a signed-in session.

`POST /api/mcp/keys` (every scope) and `POST /api/mcp/keys/ensure-default`
take `Depends(require_interactive)`: only a JWT session may create a key. A
credential minter must be at least as strict as the principal it produces.
`ensure-default` also writes the same `key_create` audit row as the create
route, so every created key is attributable.

Every refusal below goes through the REAL `get_current_user` with a key row
seeded in the real schema (`tests/db_harness.py`) and presented as a bearer —
never a dependency override, which would bypass the `scope or "user"` coercion
and the entry-point fences the real path applies. Each refusal also asserts the
`mcp_api_keys` row count did not move, so a 403 answered after a write would
still fail.

Related: docs/memory/requirements/security.md §20.10,
docs/memory/feature-flows/mcp-api-keys.md.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from db_harness import (  # noqa: E402,F401
    count as _count,
    db_backend,
    run as _hrun,
)

import dependencies  # noqa: E402
import routers.mcp_keys as _KEYS  # noqa: E402
from database import db  # noqa: E402
from db_models import McpApiKeyCreate, UserCreate  # noqa: E402

pytestmark = pytest.mark.unit

OWNER = "keymint-owner"  # admin, the default install's owner
PLAIN = "keymint-plain"  # an ordinary account
AGENT = "keymint-agent"
SYSTEM = "keymint-system"

CREATE = "/api/mcp/keys"
ENSURE = "/api/mcp/keys/ensure-default"


@pytest.fixture
def client(db_backend, monkeypatch):
    # JWT revocation is a Redis read; no Redis here.
    monkeypatch.setattr(dependencies, "get_breaker_redis", lambda: None)
    db.create_user(
        UserCreate(username=OWNER, role="admin", email="keymint-owner@example.com")
    )
    db.create_user(
        UserCreate(username=PLAIN, role="operator", email="keymint-plain@example.com")
    )
    db.register_agent_owner(AGENT, OWNER)
    app = FastAPI()
    app.include_router(_KEYS.router)
    return TestClient(app)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _jwt(username: str = OWNER) -> dict:
    return _auth(dependencies.create_access_token({"sub": username}))


def _agent_key() -> dict:
    return _auth(db.create_agent_mcp_api_key(AGENT, OWNER).api_key)


def _rescoped_key(scope: str, agent_name: str) -> dict:
    """A key row carrying `scope`, created the way the platform creates the
    system and connector keys: an agent key, then its scope column set."""
    key = db.create_agent_mcp_api_key(agent_name, OWNER)
    _hrun("UPDATE mcp_api_keys SET scope = :s WHERE id = :i", s=scope, i=key.id)
    return _auth(key.api_key)


def _user_key() -> dict:
    return _auth(db.create_mcp_api_key(OWNER, McpApiKeyCreate(name="own key")).api_key)


KEY_KINDS = {
    "agent-key": _agent_key,
    "system-key": lambda: _rescoped_key("system", SYSTEM),
    "connector-key": lambda: _rescoped_key("connector", AGENT),
    "user-key": _user_key,
}


def _keys() -> int:
    return _count("mcp_api_keys")


def _create_audits() -> list[dict]:
    from sqlalchemy import text
    from db.engine import get_engine

    with get_engine().begin() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT target_id, mcp_scope, actor_email, details FROM audit_log "
                    "WHERE event_action = 'key_create' ORDER BY timestamp"
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


class TestKeyCreationRequiresSession:
    @pytest.mark.parametrize("kind", sorted(KEY_KINDS))
    @pytest.mark.parametrize("path", [CREATE, ENSURE])
    def test_a_key_caller_is_refused_and_no_key_is_written(self, client, kind, path):
        headers = KEY_KINDS[kind]()
        before = _keys()
        body = {"name": "another"} if path == CREATE else None
        res = client.post(path, json=body, headers=headers)
        assert res.status_code == 403, res.text
        assert _keys() == before
        assert _create_audits() == []

    @pytest.mark.parametrize("scope", ["ops", "portal_delegate"])
    def test_a_key_caller_is_refused_for_the_admin_scopes_too(self, client, scope):
        headers = _user_key()
        before = _keys()
        res = client.post(CREATE, json={"name": "x", "scope": scope}, headers=headers)
        assert res.status_code == 403, res.text
        assert _keys() == before

    def test_a_signed_in_session_creates_a_key_and_gets_the_secret(self, client):
        before = _keys()
        res = client.post(CREATE, json={"name": "laptop"}, headers=_jwt())
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["api_key"].startswith("trinity_mcp_")
        assert body["scope"] == "user"
        assert _keys() == before + 1
        audits = _create_audits()
        assert [a["target_id"] for a in audits] == [body["id"]]

    @pytest.mark.parametrize("scope", ["ops", "portal_delegate"])
    def test_an_admin_session_still_creates_the_admin_scopes(self, client, scope):
        res = client.post(
            CREATE, json={"name": "svc", "scope": scope}, headers=_jwt(OWNER)
        )
        assert res.status_code == 200, res.text
        assert res.json()["scope"] == scope

    def test_a_non_admin_session_still_cannot_create_a_delegate_key(self, client):
        before = _keys()
        res = client.post(
            CREATE, json={"name": "x", "scope": "portal_delegate"}, headers=_jwt(PLAIN)
        )
        assert res.status_code == 403, res.text
        assert _keys() == before

    def test_the_created_key_authenticates_as_its_owner(self, client):
        """The session path is unchanged end to end: the new key is live."""
        created = client.post(CREATE, json={"name": "laptop"}, headers=_jwt()).json()
        info = db.validate_mcp_api_key(created["api_key"])
        assert (info["user_id"], info["scope"]) == (OWNER, "user")


class TestEnsureDefault:
    def test_a_signed_in_session_gets_a_default_key_and_one_audit_row(self, client):
        before = _keys()
        res = client.post(ENSURE, headers=_jwt())
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["api_key"].startswith("trinity_mcp_")
        assert body["name"] == "Default MCP Key"
        assert _keys() == before + 1
        audits = _create_audits()
        assert len(audits) == 1
        assert audits[0]["target_id"] == body["id"]
        assert audits[0]["mcp_scope"] is None  # a session, not a key
        assert audits[0]["actor_email"] == "keymint-owner@example.com"
        assert json.loads(audits[0]["details"])["scope"] == "user"

    def test_an_existing_user_key_means_nothing_is_created_or_audited(self, client):
        db.create_mcp_api_key(PLAIN, McpApiKeyCreate(name="already"))
        before = _keys()
        res = client.post(ENSURE, headers=_jwt(PLAIN))
        assert res.status_code == 200, res.text
        assert res.json() is None
        assert _keys() == before
        assert _create_audits() == []
